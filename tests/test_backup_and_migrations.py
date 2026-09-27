"""
tests/test_backup_and_migrations.py
====================================
Schema migrations (PRAGMA user_version), automatic backups, and staged restore.

All files live under tmp_path; QSettings is redirected to temp INI files by
the root conftest.py, so the real registry and real profin.db are never used.
"""
from __future__ import annotations

import sqlite3
import zipfile
from contextlib import closing
from datetime import date, datetime, timedelta

import pytest
from PySide6.QtCore import QSettings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.database import (
    PENDING_RESTORE_NAME,
    SCHEMA_VERSION,
    apply_pending_restore,
    migrate_schema,
)
from app.db.models import (
    CATEGORY_SOURCE_MANUAL,
    CATEGORY_SOURCE_RULE,
    Account,
    AutoCategorizationRule,
    Base,
    Category,
    CategoryLevel,
    MatchType,
    Transaction,
    TransactionType,
    User,
)
from app.settings import SettingsManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_version(path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _make_legacy_db(path) -> dict:
    """Build a pre-migration database (no category_source column, version 0)."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(username="familia", password_hash="x")
        s.add(user)
        s.flush()
        account = Account(user_id=user.id, account_name="Cuenta")
        s.add(account)
        s.flush()
        super_ = Category(user_id=user.id, name="Supermercados", level=CategoryLevel.category)
        ocio = Category(user_id=user.id, name="Ocio", level=CategoryLevel.category)
        s.add_all([super_, ocio])
        s.flush()
        s.add(AutoCategorizationRule(
            user_id=user.id, category_id=super_.id,
            match_pattern="MERCADONA", match_type=MatchType.contains,
        ))

        def txn(cat_id):
            t = Transaction(
                user_id=user.id, account_id=account.id,
                transaction_date=date(2026, 9, 1), amount=-20.0,
                type=TransactionType.expense, issuer_name="MERCADONA",
                category_id=cat_id,
            )
            s.add(t)
            return t

        by_rule, by_hand, uncategorized = txn(super_.id), txn(ocio.id), txn(None)
        s.commit()
        ids = {"rule": by_rule.id, "manual": by_hand.id, "none": uncategorized.id}
    engine.dispose()

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("ALTER TABLE transactions DROP COLUMN category_source")
        for table in ("investment_transactions", "price_history", "securities", "fx_rates"):
            conn.execute(f"DROP TABLE {table}")     # added by migration 4
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    return ids


@pytest.fixture
def data_dir(tmp_path):
    """A data folder with a small real database, configured in QSettings."""
    folder = tmp_path / "data"
    folder.mkdir()
    db = folder / "profin.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE items (name TEXT)")
        conn.execute("INSERT INTO items VALUES ('original')")
        conn.commit()

    qsettings = QSettings("GEVernova", "FAMILYFIN")
    qsettings.clear()
    qsettings.setValue("settings/data_folder", str(folder))
    qsettings.sync()
    yield folder
    qsettings.clear()


def _read_items(db) -> list[str]:
    with closing(sqlite3.connect(db)) as conn:
        return [r[0] for r in conn.execute("SELECT name FROM items")]


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def test_migration_adds_category_source_and_backfills(tmp_path):
    db = tmp_path / "profin.db"
    ids = _make_legacy_db(db)

    start = migrate_schema(db)

    assert start == 0
    assert _user_version(db) == SCHEMA_VERSION
    with closing(sqlite3.connect(db)) as conn:
        sources = dict(conn.execute("SELECT id, category_source FROM transactions"))
    # Matches what the current rules would assign → rule; differs → manual.
    assert sources[ids["rule"]] == CATEGORY_SOURCE_RULE
    assert sources[ids["manual"]] == CATEGORY_SOURCE_MANUAL
    assert sources[ids["none"]] is None
    # A safety copy is written before anything is changed.
    assert list(tmp_path.glob("profin.pre-migration-v*.db"))


def test_migration_2_adds_indexes_and_rounds_money_to_cents(tmp_path):
    db = tmp_path / "profin.db"
    ids = _make_legacy_db(db)
    with closing(sqlite3.connect(db)) as conn:
        for name in (
            "ix_transactions_user_date",
            "ix_transactions_user_category",
            "ix_transactions_user_account_date",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute(
            "UPDATE transactions SET amount = -12.339999999, running_balance = 100.004 "
            "WHERE id = ?",
            (ids["none"],),
        )
        conn.commit()

    migrate_schema(db)

    with closing(sqlite3.connect(db)) as conn:
        indexes = {
            row[1] for row in conn.execute('PRAGMA index_list("transactions")')
        }
        amount, balance = conn.execute(
            "SELECT amount, running_balance FROM transactions WHERE id = ?",
            (ids["none"],),
        ).fetchone()
    assert {
        "ix_transactions_user_date",
        "ix_transactions_user_category",
        "ix_transactions_user_account_date",
    } <= indexes
    assert amount == -12.34
    assert balance == 100.0


def test_migration_2_resumes_from_version_1(tmp_path):
    db = tmp_path / "profin.db"
    _make_legacy_db(db)
    migrate_schema(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DROP INDEX ix_transactions_user_date")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    assert migrate_schema(db) == 1
    assert _user_version(db) == SCHEMA_VERSION
    with closing(sqlite3.connect(db)) as conn:
        indexes = {row[1] for row in conn.execute('PRAGMA index_list("transactions")')}
    assert "ix_transactions_user_date" in indexes


def test_migration_3_repairs_categories_owned_by_another_user(tmp_path):
    db = tmp_path / "profin.db"
    _make_legacy_db(db)
    migrate_schema(db)
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        owner = s.query(User).one()
        gasto = Category(user_id=owner.id, name="GASTO", level=CategoryLevel.type)
        s.add(gasto)
        s.flush()
        owner_luz = Category(user_id=owner.id, parent_id=gasto.id, name="Luz",
                             level=CategoryLevel.category)
        owner_gym = Category(user_id=owner.id, parent_id=gasto.id, name="Gimnasio",
                             level=CategoryLevel.category)
        other = User(username="otra", password_hash="x")
        s.add_all([owner_luz, owner_gym, other])
        s.flush()
        other_gasto = Category(user_id=other.id, name="GASTO", level=CategoryLevel.type)
        s.add(other_gasto)
        s.flush()
        other_luz = Category(user_id=other.id, parent_id=other_gasto.id, name="Luz",
                             level=CategoryLevel.category)
        account = Account(user_id=other.id, account_name="Cuenta")
        s.add_all([other_luz, account])
        s.flush()

        def txn(cat):
            t = Transaction(user_id=other.id, account_id=account.id,
                            transaction_date=date(2026, 9, 1), amount=-5.0,
                            type=TransactionType.expense, category_id=cat.id,
                            category_source=CATEGORY_SOURCE_RULE)
            s.add(t)
            return t

        remap, clear = txn(owner_luz), txn(owner_gym)
        s.commit()
        remap_id, clear_id, other_luz_id = remap.id, clear.id, other_luz.id
    engine.dispose()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    migrate_schema(db)

    with closing(sqlite3.connect(db)) as conn:
        rows = dict(
            (row[0], row[1:])
            for row in conn.execute("SELECT id, category_id, category_source FROM transactions")
        )
    assert rows[remap_id] == (other_luz_id, CATEGORY_SOURCE_RULE)
    assert rows[clear_id] == (None, None)


def test_migration_4_adds_investment_tables_without_touching_data(tmp_path):
    db = tmp_path / "profin.db"
    _make_legacy_db(db)
    migrate_schema(db)
    with closing(sqlite3.connect(db)) as conn:       # a v3 file from release 0.4.0
        conn.execute("DROP TABLE investment_transactions")
        conn.execute("DROP TABLE price_history")
        conn.execute("DROP TABLE securities")
        conn.execute("DROP TABLE fx_rates")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        before = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()

    assert migrate_schema(db) == 3
    assert _user_version(db) == SCHEMA_VERSION
    assert list(tmp_path.glob("profin.pre-migration-v*.db"))

    with closing(sqlite3.connect(db)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[1] for r in conn.execute('PRAGMA index_list("investment_transactions")')}
        after = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
    assert {"securities", "investment_transactions", "price_history", "fx_rates"} <= tables
    assert {"ix_investment_transactions_user_date",
            "ix_investment_transactions_user_security"} <= indexes
    assert after == before


def test_migration_5_adds_broker_ref_to_a_v4_database(tmp_path):
    db = tmp_path / "profin.db"
    _make_legacy_db(db)
    migrate_schema(db)
    with closing(sqlite3.connect(db)) as conn:       # a v4 file from the F1-F6 builds
        conn.execute("DROP INDEX ux_investment_transactions_broker_ref")
        conn.execute("ALTER TABLE investment_transactions DROP COLUMN broker_ref")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()

    assert migrate_schema(db) == 4
    assert _user_version(db) == SCHEMA_VERSION
    with closing(sqlite3.connect(db)) as conn:
        columns = {r[1] for r in conn.execute('PRAGMA table_info("investment_transactions")')}
        indexes = {r[1]: r[2] for r in conn.execute('PRAGMA index_list("investment_transactions")')}
    assert "broker_ref" in columns
    assert indexes["ux_investment_transactions_broker_ref"] == 1      # unique


def test_migration_is_noop_when_up_to_date(tmp_path):
    db = tmp_path / "profin.db"
    _make_legacy_db(db)
    migrate_schema(db)
    for backup in tmp_path.glob("profin.pre-migration-v*.db"):
        backup.unlink()

    assert migrate_schema(db) == SCHEMA_VERSION
    assert not list(tmp_path.glob("profin.pre-migration-v*.db"))


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def test_backup_contains_consistent_database_snapshot(data_dir, tmp_path):
    manager = SettingsManager()
    backup = manager.backup_all_data(str(tmp_path / "backups"), user_name="Ana")

    with zipfile.ZipFile(backup) as archive:
        assert "data/profin.db" in archive.namelist()
        extracted = archive.extract("data/profin.db", tmp_path / "out")
    assert _read_items(extracted) == ["original"]


def test_auto_backup_respects_interval(data_dir, tmp_path):
    manager = SettingsManager()
    backup_dir = tmp_path / "auto"
    settings = manager.load()
    settings.backup_folder = str(backup_dir)
    settings.backup_interval_hours = 24
    manager.save(settings)
    now = datetime(2026, 9, 26, 10, 0, 0)

    assert manager.run_auto_backup_if_due(now=now) is not None
    assert manager.run_auto_backup_if_due(now=now + timedelta(hours=1)) is None
    assert manager.run_auto_backup_if_due(now=now + timedelta(hours=25)) is not None


def test_auto_backup_disabled_does_nothing(data_dir, tmp_path):
    manager = SettingsManager()
    settings = manager.load()
    settings.auto_backup = False
    settings.backup_folder = str(tmp_path / "auto")
    manager.save(settings)

    assert manager.run_auto_backup_if_due() is None
    assert not (tmp_path / "auto").exists()


def test_auto_backup_prunes_only_old_automatic_backups(data_dir, tmp_path):
    manager = SettingsManager()
    backup_dir = tmp_path / "auto"
    backup_dir.mkdir()
    manual = backup_dir / "profin_backup_ana_20260101-000000.zip"
    manual.write_bytes(b"manual")
    for i in range(SettingsManager.AUTO_BACKUP_KEEP + 3):
        old = backup_dir / f"{SettingsManager.AUTO_BACKUP_PREFIX}_ana_2026010{i:02d}.zip"
        old.write_bytes(b"old")
    settings = manager.load()
    settings.backup_folder = str(backup_dir)
    manager.save(settings)

    manager.run_auto_backup_if_due(user_name="ana")

    autos = list(backup_dir.glob(f"{SettingsManager.AUTO_BACKUP_PREFIX}_*.zip"))
    assert len(autos) == SettingsManager.AUTO_BACKUP_KEEP
    assert manual.exists()


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def test_restore_is_staged_then_applied_on_next_start(data_dir, tmp_path):
    manager = SettingsManager()
    backup = manager.backup_all_data(str(tmp_path / "backups"))

    db = data_dir / "profin.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE items SET name = 'changed later'")
        conn.commit()

    assert manager.restore_all_data(str(backup)) is True
    # The live database is untouched until the next start.
    assert _read_items(db) == ["changed later"]
    assert (data_dir / PENDING_RESTORE_NAME).exists()
    # The data folder keeps pointing at this machine's location.
    assert QSettings("GEVernova", "FAMILYFIN").value("settings/data_folder") == str(data_dir)

    assert apply_pending_restore(data_dir) is True
    assert _read_items(db) == ["original"]
    assert not (data_dir / PENDING_RESTORE_NAME).exists()
    previous = list(data_dir.glob("profin.before-restore.*.db"))
    assert len(previous) == 1 and _read_items(previous[0]) == ["changed later"]


def test_invalid_staged_restore_is_discarded(tmp_path):
    (tmp_path / "profin.db").write_bytes(b"")
    pending = tmp_path / PENDING_RESTORE_NAME
    pending.write_bytes(b"not a database")

    assert apply_pending_restore(tmp_path) is False
    assert not pending.exists()


def test_restore_rejects_archive_without_database(tmp_path):
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("metadata.json", "{}")

    assert SettingsManager().restore_all_data(str(archive_path)) is False
