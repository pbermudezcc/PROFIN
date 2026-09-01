from PySide6.QtCore import QSettings
import sys
from pathlib import Path

from app.db.database import get_database_users, get_user_app_dir, get_user_data_dir
from app.settings import SettingsManager


def test_get_user_data_dir_uses_local_appdata_in_development(monkeypatch, tmp_path):
    QSettings("GEVernova", "PROFIN").clear()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    expected = (tmp_path / "PROFIN" / "data").resolve()
    assert get_user_app_dir() == Path(__file__).resolve().parents[1].resolve()
    assert get_user_data_dir() == expected
    QSettings("GEVernova", "PROFIN").clear()


def test_get_legacy_data_dir_finds_repo_data_from_bundle_path(monkeypatch, tmp_path):
    from app import db as db_module

    repo_root = tmp_path / "repo-root"
    legacy_data = repo_root / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data / "profin.db").touch()

    bundle_path = repo_root / "dist" / "PROFIN.B.0.2.3" / "_internal" / "app" / "db" / "database.py"
    bundle_path.parent.mkdir(parents=True)
    monkeypatch.setattr(db_module.database, "__file__", str(bundle_path))

    assert db_module.database.get_legacy_data_dir() == legacy_data.resolve()


def test_get_user_data_dir_uses_local_appdata_when_frozen(monkeypatch, tmp_path):
    QSettings("GEVernova", "PROFIN").clear()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    assert get_user_app_dir() == (tmp_path / "PROFIN").resolve()
    assert get_user_data_dir() == (tmp_path / "PROFIN" / "data").resolve()
    QSettings("GEVernova", "PROFIN").clear()


def test_development_uses_packaged_data_folder_setting(monkeypatch, tmp_path):
    settings = QSettings("GEVernova", "PROFIN")
    settings.clear()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    settings.setValue("settings/data_folder", str(tmp_path / "PROFIN" / "data"))

    expected = (tmp_path / "PROFIN" / "data").resolve()
    assert get_user_data_dir() == expected
    settings.clear()


def test_settings_manager_migrates_data_folder(tmp_path):
    import sqlite3

    qsettings = QSettings("GEVernova", "PROFIN")
    qsettings.clear()
    origin = tmp_path / "origin"
    target = tmp_path / "target"
    origin.mkdir()
    target.mkdir()

    conn = sqlite3.connect(origin / "profin.db")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items (name) VALUES ('alpha')")
    conn.commit()
    conn.close()
    (origin / "profin.db-wal").write_bytes(b"wal-data")

    qsettings.setValue("settings/data_folder", str(origin))
    manager = SettingsManager(qsettings)
    migrated = manager.apply_data_folder(str(target))

    assert migrated == target.resolve()
    assert qsettings.value("settings/data_folder", type=str) == str(target.resolve())
    with sqlite3.connect(target / "profin.db") as conn:
        rows = conn.execute("SELECT name FROM items").fetchall()
    assert rows == [("alpha",)]
    qsettings.clear()


def test_get_database_users_returns_sorted_users(tmp_path):
    import sqlite3

    with sqlite3.connect(tmp_path / "profin.db") as connection:
        connection.execute(
            "CREATE TABLE users (username TEXT, password_hash TEXT)"
        )
        connection.executemany(
            "INSERT INTO users VALUES (?, ?)",
            [("zeta", "hash"), ("ALFA", "hash"), ("beta", "hash")],
        )
        connection.commit()

    assert get_database_users(tmp_path) == ["ALFA", "beta", "zeta"]


def test_get_database_users_returns_empty_for_missing_database(tmp_path):
    assert get_database_users(tmp_path) == []


def test_get_user_data_dir_falls_back_to_legacy_repo_db(monkeypatch, tmp_path):
    import sqlite3
    from app import db as db_module

    qsettings = QSettings("GEVernova", "PROFIN")
    qsettings.clear()
    legacy_dir = tmp_path / "legacy-data"
    legacy_dir.mkdir()
    legacy_db = legacy_dir / "profin.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        conn.execute("INSERT INTO users (username) VALUES ('COMUN')")
        conn.commit()

    app_data = tmp_path / "AppData" / "Local" / "PROFIN" / "data"
    app_data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(app_data / "profin.db")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setattr(db_module.database, "get_legacy_data_dir", lambda: legacy_dir)
    qsettings.setValue("settings/data_folder", "")

    result = db_module.database.get_user_data_dir()

    assert result == app_data.resolve()
    assert (app_data / "profin.db").exists()
    with sqlite3.connect(app_data / "profin.db") as conn:
        assert conn.execute("SELECT username FROM users WHERE username = 'COMUN'").fetchone() == ("COMUN",)
    qsettings.clear()


def test_get_user_data_dir_overwrites_empty_packaged_db_with_legacy_users(monkeypatch, tmp_path):
    import sqlite3
    from app import db as db_module

    qsettings = QSettings("GEVernova", "PROFIN")
    qsettings.clear()

    legacy_dir = tmp_path / "legacy-data"
    legacy_dir.mkdir()
    with sqlite3.connect(legacy_dir / "profin.db") as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)")
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES ('COMUN', '$2b$12$test-hash')"
        )
        conn.commit()

    app_data = tmp_path / "AppData" / "Local" / "PROFIN" / "data"
    app_data.mkdir(parents=True, exist_ok=True)
    app_db = app_data / "profin.db"
    app_db.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setattr(db_module.database, "get_legacy_data_dir", lambda: legacy_dir)
    qsettings.setValue("settings/data_folder", "")

    result = db_module.database.get_user_data_dir()

    assert result == app_data.resolve()
    with sqlite3.connect(app_db) as conn:
        assert conn.execute("SELECT username FROM users WHERE username = 'COMUN'").fetchone() == ("COMUN",)
    qsettings.clear()
