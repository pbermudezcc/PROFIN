"""
tests/test_schema_and_startup.py
=================================
Release-critical database guarantees:
  - a database created fresh and one upgraded from the oldest shape end up
    with exactly the same tables, columns and indexes (no drift between
    Transaction.__table_args__ / models and the migrations);
  - init_db() on a real data folder creates, migrates and re-opens it
    idempotently;
  - every module of the app imports (what the packaged build must load).
"""
from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from contextlib import closing

import pytest
from PySide6.QtCore import QSettings
from sqlalchemy import create_engine

import app
from app.db import database
from app.db.models import Base
from tests.test_backup_and_migrations import _make_legacy_db


def _schema(path) -> dict:
    with closing(sqlite3.connect(path)) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {
            "version": conn.execute("PRAGMA user_version").fetchone()[0],
            "columns": {t: sorted(r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')) for t in tables},
            "indexes": {t: sorted(r[1] for r in conn.execute(f'PRAGMA index_list("{t}")')
                                  if not r[1].startswith("sqlite_autoindex")) for t in tables},
        }


def test_fresh_and_upgraded_databases_have_identical_schema(tmp_path):
    fresh = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{fresh}")
    Base.metadata.create_all(engine)
    engine.dispose()
    database.migrate_schema(fresh, backup_first=False)

    upgraded = tmp_path / "old.db"
    _make_legacy_db(upgraded)
    with closing(sqlite3.connect(upgraded)) as conn:     # oldest shape: no indexes
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'").fetchall():
            conn.execute(f"DROP INDEX {name}")
        conn.commit()
    database.migrate_schema(upgraded, backup_first=False)

    assert _schema(fresh) == _schema(upgraded)
    assert _schema(fresh)["version"] == database.SCHEMA_VERSION


def test_every_model_column_exists_after_migrations(tmp_path):
    db = tmp_path / "old.db"
    _make_legacy_db(db)
    database.migrate_schema(db, backup_first=False)
    columns = _schema(db)["columns"]
    for table in Base.metadata.sorted_tables:
        assert sorted(c.name for c in table.columns) == columns[table.name], table.name


@pytest.fixture
def data_folder(tmp_path):
    """Point the app at an empty temp data folder; restore the bindings after."""
    folder = tmp_path / "datos"
    settings = QSettings("GEVernova", "FAMILYFIN")
    settings.clear()
    settings.setValue("settings/data_folder", str(folder))
    settings.sync()
    yield folder
    from conftest import point_app_at_test_data_dir

    settings.clear()
    point_app_at_test_data_dir()
    database.refresh_runtime_paths()


def test_init_db_creates_migrates_and_reopens_idempotently(data_folder):
    database.init_db()

    db = data_folder / "profin.db"
    assert database.DB_PATH == db
    assert _schema(db)["version"] == database.SCHEMA_VERSION
    assert database.get_database_users(data_folder) == []   # no demo users
    # A brand-new file is not "migrated", so no pre-migration copy is made.
    assert not list(data_folder.glob("profin.pre-migration-*.db"))

    # The session must be bound to the new folder, not the import-time one.
    with database.get_session() as session:
        assert session.get_bind().url.database == str(db)
        from app.auth.auth_service import create_user
        create_user(session, "ana", "secreto1")
    database.init_db()

    assert database.get_database_users(data_folder) == ["ana"]
    assert not list(data_folder.glob("profin.pre-migration-*.db"))


def test_init_db_upgrades_an_old_database_with_a_safety_copy(data_folder):
    data_folder.mkdir()
    _make_legacy_db(data_folder / "profin.db")

    database.init_db()

    assert _schema(data_folder / "profin.db")["version"] == database.SCHEMA_VERSION
    assert len(list(data_folder.glob("profin.pre-migration-*.db"))) == 1
    assert database.get_database_users(data_folder) == ["familia"]


def _app_modules():
    return sorted(
        info.name for info in pkgutil.walk_packages(app.__path__, prefix="app.")
    )


@pytest.mark.parametrize("module_name", _app_modules())
def test_every_app_module_imports(module_name):
    importlib.import_module(module_name)


def test_entry_point_imports():
    import main
    assert callable(main.main)
