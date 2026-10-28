"""
Unit tests for authentication service (Phase 1).

Tests:
  - create_user: happy path, duplicate username, empty inputs, shared policy
  - name and password rules (account_rules)
  - verify_login: correct credentials, wrong password, unknown user (same bcrypt cost)
  - last_login updated on successful login
  - change_password / rename_user / deletion_summary / delete_user (account_service)
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Account,
    AuditLog,
    AutoCategorizationRule,
    Base,
    Category,
    CategoryLevel,
    MatchType,
    Transaction,
    TransactionType,
    User,
)
from app.auth import auth_service
from app.auth.account_rules import AccountError, name_key
from app.auth.account_service import change_password, delete_user, deletion_summary, rename_user
from app.auth.auth_service import create_user, validate_new_user, verify_login
from app.db.database import ensure_demo_users


# ---------------------------------------------------------------------------
# Fixtures — in-memory SQLite DB per test
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Provide a fresh in-memory SQLite session for each test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# create_user tests
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_create_user_happy_path(self, db_session):
        user = create_user(db_session, "pedro", "huerto-de-limones")
        assert user.id is not None
        assert user.username == "pedro"
        assert user.password_hash != "huerto-de-limones"  # never store plaintext
        assert user.password_hash.startswith("$2b$")  # bcrypt prefix

    def test_create_user_stores_trimmed_nfc_name(self, db_session):
        decomposed = "Lucía"                       # «Lucía» typed as i + combining accent
        user = create_user(db_session, f"  {decomposed} ", "huerto-de-limones")
        assert user.username == "Lucía" and len(user.username) == 5

    def test_create_user_duplicate_raises(self, db_session):
        create_user(db_session, "pedro", "huerto-de-limones")
        with pytest.raises(AccountError, match="Ya existe") as exc:
            create_user(db_session, "Pedro", "otra-clave-larga")
        assert exc.value.field == "username"

    def test_create_user_empty_username_raises(self, db_session):
        with pytest.raises(ValueError, match="nombre de usuario"):
            create_user(db_session, "   ", "huerto-de-limones")

    def test_create_user_empty_password_raises(self, db_session):
        with pytest.raises(ValueError, match="contraseña"):
            create_user(db_session, "pedro", "")

    def test_create_user_applies_the_password_policy(self, db_session):
        with pytest.raises(AccountError, match="al menos 8") as exc:
            create_user(db_session, "pedro", "1234567")
        assert exc.value.field == "password"
        with pytest.raises(AccountError, match="demasiado común"):
            create_user(db_session, "pedro", "Password123")

    def test_create_user_password_over_bcrypt_limit_raises_in_spanish(self, db_session):
        # bcrypt 5 raises an English ValueError above 72 bytes; never show it.
        with pytest.raises(ValueError, match="demasiado larga"):
            create_user(db_session, "pedro", "ñ" * 37)          # 74 UTF-8 bytes
        with pytest.raises(ValueError, match="demasiado larga"):
            create_user(db_session, "pedro", "ñ" * 37, enforce_policy=False)

    def test_trusted_tooling_can_create_the_demo_account(self, db_session):
        user = create_user(db_session, "DEMO", "123456", enforce_policy=False)
        assert user.username == "DEMO"


# ---------------------------------------------------------------------------
# Name and password rules (shared by the desktop dialog and the web)
# ---------------------------------------------------------------------------

class TestValidateNewUser:
    def test_valid_account_has_no_errors(self, db_session):
        create_user(db_session, "ana", "clave-ana-larga")
        assert validate_new_user(db_session, "  Lucía ", "huerto-de-limones", "huerto-de-limones") == {}
        assert validate_new_user(db_session, "José_M. Pérez-2", "huerto-de-limones", "huerto-de-limones") == {}

    @pytest.mark.parametrize("username, message", [
        ("", "Escribe un nombre"),
        ("   ", "Escribe un nombre"),
        ("x", "al menos 2"),
        ("x" * 33, "como máximo 32"),
        ("ana\tbeto", "Usa solo letras"),
        ("ana@casa", "Usa solo letras"),
        ("ANA", "Ya existe el usuario «ana»"),
        ("Ána", "Ya existe el usuario «ana»"),          # accents ignored too
        ("demo", "reservado"),
        ("Démo", "reservado"),
    ])
    def test_username_rules(self, db_session, username, message):
        create_user(db_session, "ana", "clave-ana-larga")
        errors = validate_new_user(db_session, username, "huerto-de-limones", "huerto-de-limones")
        assert message in errors["username"]
        assert set(errors) == {"username"}

    @pytest.mark.parametrize("password, confirm, field, message", [
        ("", "", "password", "Escribe una contraseña"),
        ("1234567", "1234567", "password", "al menos 8"),
        ("a" * 65, "a" * 65, "password", "como máximo 64"),
        ("ñ" * 37, "ñ" * 37, "password", "demasiado larga"),     # 37 chars, 74 bytes
        ("Lucía-Pérez", "Lucía-Pérez", "password", "nombre de usuario"),
        ("barcelona", "barcelona", "password", "demasiado común"),
        ("Contraseña123", "Contraseña123", "password", "demasiado común"),
        ("huerto-de-limones", "huerto-de-limon", "confirm", "no coinciden"),
    ])
    def test_password_rules(self, db_session, password, confirm, field, message):
        errors = validate_new_user(db_session, "Lucía-Pérez", password, confirm)
        assert message in errors[field]
        assert set(errors) == {field}

    def test_eight_characters_is_enough_and_72_bytes_is_the_limit(self, db_session):
        assert validate_new_user(db_session, "lucia", "zx8!kq2m", "zx8!kq2m") == {}
        assert validate_new_user(db_session, "lucia", "k" * 64, "k" * 64) == {}
        assert validate_new_user(db_session, "lucia", "ñ" * 36, "ñ" * 36) == {}     # 72 bytes
        assert "password" in validate_new_user(db_session, "lucia", "ñ" * 37, "ñ" * 37)

    def test_name_key_ignores_case_and_accents(self):
        assert name_key("  LUCÍA ") == name_key("lucia") == name_key("Lucía")


# ---------------------------------------------------------------------------
# verify_login tests
# ---------------------------------------------------------------------------

class TestVerifyLogin:
    def test_login_correct_credentials(self, db_session):
        create_user(db_session, "maria", "huerto-de-limones")
        user = verify_login(db_session, "maria", "huerto-de-limones")
        assert user is not None
        assert user.username == "maria"

    def test_login_wrong_password_returns_none(self, db_session):
        create_user(db_session, "maria", "huerto-de-limones")
        result = verify_login(db_session, "maria", "wrongpassword")
        assert result is None

    def test_login_unknown_user_returns_none(self, db_session):
        result = verify_login(db_session, "fantasma", "anything")
        assert result is None

    def test_unknown_user_costs_one_bcrypt_check_like_a_known_one(self, db_session, monkeypatch):
        create_user(db_session, "maria", "huerto-de-limones")
        calls = []
        real = auth_service._verify_password
        monkeypatch.setattr(auth_service, "_verify_password",
                            lambda plain, hashed: calls.append(hashed) or real(plain, hashed))
        assert verify_login(db_session, "fantasma", "anything") is None
        assert verify_login(db_session, "maria", "anything") is None
        assert len(calls) == 2
        assert calls[0] == auth_service._DUMMY_HASH and calls[0].startswith("$2b$12$")
        assert calls[1].startswith("$2b$12$")

    def test_existing_short_passwords_keep_working(self, db_session):
        create_user(db_session, "comun", "pass123", enforce_policy=False)
        user = verify_login(db_session, "comun", "pass123")
        assert user.last_login is not None


# ---------------------------------------------------------------------------
# account_service: change password, rename, delete
# ---------------------------------------------------------------------------

def _family(db_session):
    """ana with an account, a category, a transaction and a personal rule; beto apart."""
    ana = create_user(db_session, "ana", "clave-ana-larga")
    beto = create_user(db_session, "beto", "clave-beto-larga")
    account = Account(user_id=ana.id, account_name="Cuenta ana")
    gasto = Category(user_id=ana.id, name="GASTO", level=CategoryLevel.type, transaction_type=TransactionType.expense)
    db_session.add_all([account, gasto])
    db_session.flush()
    cat = Category(user_id=ana.id, parent_id=gasto.id, name="Super", level=CategoryLevel.category)
    db_session.add(cat)
    db_session.flush()
    db_session.add_all([
        Transaction(user_id=ana.id, account_id=account.id, transaction_date=date(2026, 1, 2),
                    amount=-10.0, type=TransactionType.expense, category_id=cat.id),
        AutoCategorizationRule(user_id=ana.id, category_id=cat.id, match_pattern="MERCA",
                               match_type=MatchType.contains, priority=100),
    ])
    db_session.commit()
    return ana, beto, cat


class TestChangePassword:
    def test_needs_the_current_password(self, db_session):
        ana, _, _ = _family(db_session)
        with pytest.raises(AccountError) as exc:
            change_password(db_session, ana.id, "mal", "huerto-de-limones", "huerto-de-limones")
        assert exc.value.field == "current"
        assert verify_login(db_session, "ana", "clave-ana-larga") is not None

    def test_new_password_follows_the_rules(self, db_session):
        ana, _, _ = _family(db_session)
        with pytest.raises(AccountError, match="al menos 8"):
            change_password(db_session, ana.id, "clave-ana-larga", "corta", "corta")
        with pytest.raises(AccountError, match="distinta de la actual"):
            change_password(db_session, ana.id, "clave-ana-larga", "clave-ana-larga", "clave-ana-larga")

    def test_changes_the_password(self, db_session):
        ana, _, _ = _family(db_session)
        change_password(db_session, ana.id, "clave-ana-larga", "huerto-de-limones", "huerto-de-limones")
        assert verify_login(db_session, "ana", "clave-ana-larga") is None
        assert verify_login(db_session, "ana", "huerto-de-limones") is not None


class TestRenameUser:
    def test_rename_with_password(self, db_session):
        ana, _, _ = _family(db_session)
        assert rename_user(db_session, ana.id, "clave-ana-larga", " Ana María ") == ("ana", "Ana María")
        assert verify_login(db_session, "Ana María", "clave-ana-larga") is not None

    def test_own_name_with_other_case_is_allowed(self, db_session):
        ana, _, _ = _family(db_session)
        assert rename_user(db_session, ana.id, "clave-ana-larga", "Ana")[1] == "Ana"

    @pytest.mark.parametrize("new_name, password, field", [
        ("Ana María", "mal", "password"),
        ("BETO", "clave-ana-larga", "username"),
        ("demo", "clave-ana-larga", "username"),
        ("ana", "clave-ana-larga", "username"),
    ])
    def test_refused(self, db_session, new_name, password, field):
        ana, _, _ = _family(db_session)
        with pytest.raises(AccountError) as exc:
            rename_user(db_session, ana.id, password, new_name)
        assert exc.value.field == field
        assert db_session.get(User, ana.id).username == "ana"

    def test_demo_account_cannot_be_renamed(self, db_session):
        demo = create_user(db_session, "DEMO", "123456", enforce_policy=False)
        with pytest.raises(AccountError, match="demostración"):
            rename_user(db_session, demo.id, "123456", "Visitante")


class TestDeleteUser:
    def test_summary_counts_the_users_data(self, db_session):
        ana, _, _ = _family(db_session)
        summary = deletion_summary(db_session, ana.id)
        assert (summary.accounts, summary.transactions, summary.categories, summary.rules) == (1, 1, 2, 1)
        assert summary.global_rules == 0 and not summary.blocked

    def test_wrong_name_or_password_deletes_nothing(self, db_session):
        ana, _, _ = _family(db_session)
        backups = []
        for name, password, field in (("Ana", "clave-ana-larga", "confirm_name"), ("ana", "mal", "password")):
            with pytest.raises(AccountError) as exc:
                delete_user(db_session, ana.id, password, name, backup=lambda: backups.append(1))
            assert exc.value.field == field
        assert backups == [] and db_session.get(User, ana.id) is not None

    def test_failed_backup_deletes_nothing(self, db_session):
        ana, _, _ = _family(db_session)

        def broken_backup():
            raise OSError("disco lleno")

        with pytest.raises(AccountError, match="no se ha borrado nada"):
            delete_user(db_session, ana.id, "clave-ana-larga", "ana", backup=broken_backup)
        assert db_session.get(User, ana.id) is not None
        assert db_session.query(Transaction).count() == 1

    def test_refused_while_family_rules_use_its_categories(self, db_session):
        ana, _, cat = _family(db_session)
        db_session.add(AutoCategorizationRule(user_id=None, category_id=cat.id, match_pattern="GLOBAL",
                                              match_type=MatchType.contains, priority=100))
        db_session.commit()
        assert deletion_summary(db_session, ana.id).global_rules == 1
        backups = []
        with pytest.raises(AccountError, match="reglas globales"):
            delete_user(db_session, ana.id, "clave-ana-larga", "ana", backup=lambda: backups.append(1))
        assert backups == [] and db_session.get(User, ana.id) is not None

    def test_deletes_everything_and_leaves_a_surviving_audit_row(self, db_session, tmp_path):
        ana, beto, _ = _family(db_session)
        ana_id = ana.id
        db_session.add(AuditLog(user_id=ana_id, table_affected="categories", action="insert", new_value="x"))
        db_session.commit()
        copy = tmp_path / "profin_predelete_ana.zip"

        result = delete_user(db_session, ana_id, "clave-ana-larga", "ana", backup=lambda: copy, source="web")

        assert result == copy
        assert db_session.get(User, ana_id) is None
        for model in (Account, Category, Transaction, AutoCategorizationRule):
            assert db_session.query(model).filter(model.user_id == ana_id).count() == 0
        entry = db_session.query(AuditLog).filter(AuditLog.table_affected == "users").one()
        assert entry.user_id is None and entry.record_id == ana_id
        assert "«ana»" in entry.old_value and copy.name in entry.new_value and "web" in entry.new_value
        assert db_session.query(AuditLog).filter(AuditLog.user_id == ana_id).count() == 0
        assert verify_login(db_session, "beto", "clave-beto-larga") is not None


def test_ensure_demo_users_does_not_create_automatic_credentials(db_session):
    created = ensure_demo_users(db_session)

    assert created == []
    assert verify_login(db_session, "usuario", "123456") is None
    assert verify_login(db_session, "admin", "123456") is None