# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT
import os
from pathlib import Path

ROOT = Path.cwd()
APP_NAME = "PROFIN"
APP_CHANNEL = os.environ.get("PROFIN_CHANNEL", "B").upper()


def _read_app_version() -> str:
    config_text = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("APP_VERSION"):
            return stripped.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("No se pudo leer APP_VERSION en app/config.py")


APP_VERSION = _read_app_version()
ARTIFACT_NAME = f"{APP_NAME}.{APP_CHANNEL}.{APP_VERSION}"

block_cipher = None

analysis = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "app"), "app"),
    ],
    hiddenimports=[
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtWidgets",
        "PySide6.QtGui",
        "sqlalchemy",
        "qtawesome",
        "app",
        "app.auth",
        "app.categorization",
        "app.db",
        "app.gui",
        "app.ingestion",
        "app.settings",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=ARTIFACT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=ARTIFACT_NAME,
)
