"""
tests/test_category_manager.py
===============================
First-run seeding and category CRUD — what "Cargar categorías por defecto"
and the Categorías screen rely on.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db.database import _set_sqlite_pragma

from app.auth.auth_service import create_user
from app.categorization.category_manager import (
    assign_transaction_category,
    create_category,
    delete_category,
    get_all_categories,
    seed_default_categories,
    seed_default_rules,
    similar_uncategorized_ids,
    update_category_name,
)
from app.db.models import (
    CATEGORY_SOURCE_MANUAL, Account, AutoCategorizationRule, Base, Category,
    CategoryLevel, Transaction, TransactionType,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    # Same connection setup as the app: ON DELETE SET NULL / CASCADE need
    # PRAGMA foreign_keys=ON.
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def user_id(session):
    return create_user(session, "ana", "clave-secreta-1").id


def test_seeding_is_idempotent_and_per_user(session, user_id):
    created = seed_default_categories(session, user_id)
    rules = seed_default_rules(session, user_id)
    assert created > 0 and rules > 0

    assert seed_default_categories(session, user_id) == 0
    assert seed_default_rules(session, user_id) == 0

    types = {c.name for c in get_all_categories(session, user_id, level=CategoryLevel.type)}
    assert types == {"GASTO", "INGRESO", "SERV. BANCARIO", "INVERSION"}
    other = create_user(session, "pedro", "clave-secreta-1").id
    assert get_all_categories(session, other) == []


def test_seeded_rules_point_to_the_users_own_categories(session, user_id):
    seed_default_categories(session, user_id)
    seed_default_rules(session, user_id)
    for rule in session.query(AutoCategorizationRule).all():
        assert rule.user_id == user_id
        assert rule.category.user_id == user_id
        assert rule.category.level == CategoryLevel.category


def test_duplicate_names_resolve_to_the_right_type(session, user_id):
    seed_default_categories(session, user_id)
    seed_default_rules(session, user_id)
    hbo_rule = (
        session.query(AutoCategorizationRule)
        .filter(AutoCategorizationRule.match_pattern == "HBO").one()
    )
    assert hbo_rule.category.parent.name == "GASTO"


def test_delete_category_uncategorizes_transactions_and_removes_rules(session, user_id):
    seed_default_categories(session, user_id)
    seed_default_rules(session, user_id)
    luz = next(c for c in get_all_categories(session, user_id) if c.name == "Luz")
    account = Account(user_id=user_id, account_name="Cuenta")
    session.add(account)
    session.flush()
    txn = Transaction(user_id=user_id, account_id=account.id,
                      transaction_date=date(2026, 9, 1), amount=-40.0,
                      type=TransactionType.expense, category_id=luz.id)
    session.add(txn)
    session.commit()

    delete_category(session, luz.id)

    session.refresh(txn)
    assert txn.category_id is None
    assert session.query(AutoCategorizationRule).filter_by(category_id=luz.id).count() == 0


def test_create_rename_and_validation(session, user_id):
    gasto = create_category(session, user_id, "GASTO", CategoryLevel.type,
                            transaction_type=TransactionType.expense)
    cat = create_category(session, user_id, "  Mascotas ", CategoryLevel.category, parent_id=gasto.id)
    assert cat.name == "Mascotas"
    assert update_category_name(session, cat.id, "Veterinario").name == "Veterinario"
    with pytest.raises(ValueError):
        create_category(session, user_id, "Sin padre", CategoryLevel.category)
    with pytest.raises(ValueError):
        update_category_name(session, cat.id, "   ")


def test_manual_assignment_is_marked_and_clearing_resets_source(session, user_id):
    gasto = create_category(session, user_id, "GASTO", CategoryLevel.type)
    cat = create_category(session, user_id, "Varios", CategoryLevel.category, parent_id=gasto.id)
    account = Account(user_id=user_id, account_name="Cuenta")
    session.add(account)
    session.flush()
    txn = Transaction(user_id=user_id, account_id=account.id,
                      transaction_date=date(2026, 9, 1), amount=-1.0,
                      type=TransactionType.expense)
    session.add(txn)
    session.commit()

    assign_transaction_category(session, txn.id, cat.id)
    assert (txn.category_id, txn.category_source) == (cat.id, CATEGORY_SOURCE_MANUAL)
    assign_transaction_category(session, txn.id, None)
    assert (txn.category_id, txn.category_source) == (None, None)
    with pytest.raises(ValueError):
        assign_transaction_category(session, 999_999, cat.id)


def test_apply_to_similar_only_touches_same_merchant_type_and_uncategorized(session, user_id):
    gasto = create_category(session, user_id, "GASTO", CategoryLevel.type)
    cat = create_category(session, user_id, "Supermercado", CategoryLevel.category, parent_id=gasto.id)
    other = create_category(session, user_id, "Varios", CategoryLevel.category, parent_id=gasto.id)
    account = Account(user_id=user_id, account_name="Cuenta")
    session.add(account)
    session.flush()

    def txn(issuer, amount=-10.0, kind=TransactionType.expense, concept=None, category_id=None, owner=user_id):
        t = Transaction(user_id=owner, account_id=account.id, transaction_date=date(2026, 9, 1),
                        amount=amount, type=kind, issuer_name=issuer, concept=concept,
                        category_id=category_id)
        session.add(t)
        session.flush()
        return t

    target = txn("EROSKI CITY URQUI", concept="EROSKI CITY URQUI | 04000022SIO COMPRA CON TARJETA")
    same = txn("eroski  city urqui ", concept="EROSKI CITY URQUI | 99999999XXX COMPRA CON TARJETA")
    refund = txn("EROSKI CITY URQUI", amount=5.0, kind=TransactionType.income)
    already = txn("EROSKI CITY URQUI", category_id=other.id)
    different = txn("EROSKI CENTER")
    pedro = create_user(session, "pedro", "clave-secreta-1").id
    foreign = txn("EROSKI CITY URQUI", owner=pedro)
    no_issuer = txn(None, concept="TRASPASO TARJETA DE CREDITO")
    no_issuer_same = txn(None, concept="Traspaso tarjeta de credito")
    session.commit()

    assert similar_uncategorized_ids(session, target.id) == [same.id]
    assert similar_uncategorized_ids(session, no_issuer.id) == [no_issuer_same.id]

    # Unticked: only the chosen movement changes.
    assert assign_transaction_category(session, target.id, cat.id) == 1
    assert same.category_id is None

    assert assign_transaction_category(session, target.id, cat.id, apply_to_similar=True) == 2
    assert (same.category_id, same.category_source) == (cat.id, CATEGORY_SOURCE_MANUAL)
    assert refund.category_id is None and different.category_id is None and foreign.category_id is None
    assert already.category_id == other.id

    # Clearing never spreads to similar movements.
    assert assign_transaction_category(session, target.id, None, apply_to_similar=True) == 1
    assert same.category_id == cat.id


def test_rule_from_transaction_is_offered_once_and_fills_uncategorized(session, user_id):
    from app.categorization.admin_service import NotFound, add_rule_from_transaction, transaction_has_rule
    from app.db.models import CATEGORY_SOURCE_RULE, MatchType

    gasto = create_category(session, user_id, "GASTO", CategoryLevel.type)
    cat = create_category(session, user_id, "Supermercado", CategoryLevel.category, parent_id=gasto.id)
    account = Account(user_id=user_id, account_name="Cuenta")
    session.add(account)
    session.flush()
    txns = [Transaction(user_id=user_id, account_id=account.id, transaction_date=date(2026, 9, 1),
                        amount=-10.0, type=TransactionType.expense, issuer_name=issuer,
                        concept=f"{issuer} | 04000022SIO COMPRA CON TARJETA")
            for issuer in ("EROSKI CITY URQUI", "EROSKI CITY URQUI", "EROSKI CENTER")]
    session.add_all(txns)
    session.commit()
    target, twin, other = txns

    assert not transaction_has_rule(session, user_id, target.id)
    created = add_rule_from_transaction(session, user_id, target.id, cat.id)
    assert created is not None
    rule = created.rule
    assert (rule.match_pattern, rule.match_type, rule.user_id) == ("EROSKI CITY URQUI", MatchType.exact, user_id)
    assert set(created.categorized_ids) == {target.id, twin.id}
    assert (twin.category_id, twin.category_source) == (cat.id, CATEGORY_SOURCE_RULE)
    assert other.category_id is None                      # exact match only

    # A rule now covers it: not offered again, nothing new created.
    assert transaction_has_rule(session, user_id, twin.id)
    assert add_rule_from_transaction(session, user_id, twin.id, cat.id) is None
    assert session.query(AutoCategorizationRule).count() == 1

    pedro = create_user(session, "pedro", "clave-secreta-1").id
    with pytest.raises(NotFound):
        add_rule_from_transaction(session, pedro, other.id, cat.id)
