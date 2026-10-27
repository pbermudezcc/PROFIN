"""
tools/demo/run_demo.py
======================
FAMILYFIN — Opens the desktop app on the DEMO data (user DEMO / 123456).

The family's database and preferences are never touched: _sandbox.py
redirects the data folder and every QSettings to dist/demo/. Builds the
demo first if it does not exist yet.

    python tools/demo/run_demo.py
    python tools/demo/run_demo.py --rebuild    # regenerate the demo data first
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import _sandbox  # noqa: F401  (must run before any app import)
from _sandbox import DATA_DIR, DEMO_USER, ROOT, assert_isolated

if "--rebuild" in sys.argv or not (DATA_DIR / "profin.db").exists():
    subprocess.run([sys.executable, str(Path(__file__).with_name("build_demo.py"))], check=True)
    sys.argv = [a for a in sys.argv if a != "--rebuild"]

from PySide6.QtCore import QSettings  # noqa: E402  (already the sandboxed class)

settings = QSettings("FAMILYFIN", "FAMILYFIN")
settings.setValue("auth/last_username", DEMO_USER)
settings.sync()

assert_isolated()
sys.path.insert(0, str(ROOT))
import main  # noqa: E402

main.main()
