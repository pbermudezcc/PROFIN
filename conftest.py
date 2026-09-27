"""
Test-wide isolation from the real machine.

QSettings(org, app) writes to the Windows registry (HKCU\\Software\\GEVernova\\
FAMILYFIN), and several tests call .clear() on it. Without this redirect, running
pytest wipes the family's real app preferences. Every QSettings(org, app) built
during tests is sent to INI files under a throwaway temp folder instead.

This runs before any test module (or app module) imports QSettings.
"""
import tempfile

import PySide6.QtCore as _qtcore

_RealQSettings = _qtcore.QSettings
_SETTINGS_DIR = tempfile.mkdtemp(prefix="familyfin-test-settings-")
_RealQSettings.setPath(
    _RealQSettings.Format.IniFormat, _RealQSettings.Scope.UserScope, _SETTINGS_DIR
)


class _IsolatedQSettings(_RealQSettings):
    def __init__(self, *args, **kwargs):
        if len(args) == 2 and all(isinstance(a, str) for a in args) and not kwargs:
            super().__init__(
                _RealQSettings.Format.IniFormat,
                _RealQSettings.Scope.UserScope,
                *args,
            )
        else:
            super().__init__(*args, **kwargs)


_qtcore.QSettings = _IsolatedQSettings


# ---------------------------------------------------------------------------
# Real family data isolation
# ---------------------------------------------------------------------------
# app.db.database binds its engine to the data folder at import time, and a
# stale binding once made a test write a user into the family's real
# profin.db. Before any app module is imported, point both the configured
# data folder and %LOCALAPPDATA% at a throwaway directory, and fail any test
# that leaves the app bound to the real database.
import os
from pathlib import Path

import pytest

_REAL_DATA_DIR = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    / "FAMILYFIN"
).resolve()
_TEST_HOME = Path(tempfile.mkdtemp(prefix="familyfin-test-home-"))
TEST_DATA_DIR = _TEST_HOME / "FAMILYFIN" / "data"
os.environ["LOCALAPPDATA"] = str(_TEST_HOME)


def point_app_at_test_data_dir() -> None:
    settings = _IsolatedQSettings("GEVernova", "FAMILYFIN")
    settings.setValue("settings/data_folder", str(TEST_DATA_DIR))
    settings.sync()


point_app_at_test_data_dir()


@pytest.fixture(autouse=True)
def _never_bound_to_real_family_data():
    yield
    import sys

    database = sys.modules.get("app.db.database")
    if database is None:
        return
    for label, value in (
        ("DB_PATH", database.DB_PATH),
        ("engine", database.engine.url.database),
        ("SessionLocal", database.SessionLocal.kw["bind"].url.database),
    ):
        path = Path(str(value)).resolve()
        assert _REAL_DATA_DIR not in path.parents, (
            f"Test left app.db.database.{label} pointing at real family data: {path}"
        )
