"""
tools/demo/_sandbox.py
======================
FAMILYFIN — Keeps the DEMO completely apart from the family's real data.

Import this module BEFORE any `app.*` module (they bind QSettings and the
database file at import time). It does the same isolation the test suite
does in conftest.py:

  * QSettings("FAMILYFIN", "FAMILYFIN") → an INI file inside the demo folder
    instead of the Windows registry (theme, last user, data folder, backups…)
  * %LOCALAPPDATA% → the demo folder, so default paths never reach
    %LOCALAPPDATA%\\FAMILYFIN
  * settings/data_folder → <demo>/FAMILYFIN/data (where profin.db lives)

The demo folder defaults to dist/demo/ (gitignored) and can be changed
with the FAMILYFIN_DEMO_HOME environment variable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import PySide6.QtCore as _qtcore

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_HOME = Path(os.environ.get("FAMILYFIN_DEMO_HOME") or ROOT / "dist" / "demo").resolve()
DATA_DIR = DEMO_HOME / "FAMILYFIN" / "data"
SETTINGS_DIR = DEMO_HOME / "settings"
STATEMENTS_DIR = DEMO_HOME / "extractos"
BACKUP_DIR = DEMO_HOME / "FAMILYFIN" / "backups"

DEMO_USER = "DEMO"        # app.config.DEMO_USERNAME
DEMO_PASSWORD = "123456"

_REAL_FAMILY_DIR = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "FAMILYFIN"
).resolve()

if "app.db.database" in sys.modules:
    raise RuntimeError("tools/demo/_sandbox.py must be imported before any app module.")

for folder in (DATA_DIR, SETTINGS_DIR, STATEMENTS_DIR, BACKUP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

_RealQSettings = _qtcore.QSettings
_RealQSettings.setPath(_RealQSettings.Format.IniFormat, _RealQSettings.Scope.UserScope, str(SETTINGS_DIR))


class _DemoQSettings(_RealQSettings):
    def __init__(self, *args, **kwargs):
        if len(args) == 2 and all(isinstance(a, str) for a in args) and not kwargs:
            super().__init__(_RealQSettings.Format.IniFormat, _RealQSettings.Scope.UserScope, *args)
        else:
            super().__init__(*args, **kwargs)


_qtcore.QSettings = _DemoQSettings
os.environ["LOCALAPPDATA"] = str(DEMO_HOME)

_settings = _DemoQSettings("FAMILYFIN", "FAMILYFIN")
_settings.setValue("settings/data_folder", str(DATA_DIR))
_settings.setValue("settings/backup_folder", str(BACKUP_DIR))
_settings.setValue("settings/import_folder", str(STATEMENTS_DIR))
_settings.setValue("web/enabled", False)
_settings.sync()


def assert_isolated() -> None:
    """Abort if, for any reason, the app ended up bound to the real data."""
    from app.db import database

    db_path = Path(str(database.DB_PATH)).resolve()
    if _REAL_FAMILY_DIR in db_path.parents or DEMO_HOME not in db_path.parents:
        raise RuntimeError(f"La demo no está aislada: la base de datos apunta a {db_path}")
