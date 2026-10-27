"""
tools/demo/build_demo.py
========================
FAMILYFIN — Builds the isolated DEMO sandbox (user DEMO / 123456) with the
persona of demo_data.py.

Never touches the family's data (see _sandbox.py): everything is written
under dist/demo/ (or FAMILYFIN_DEMO_HOME). This is what the manual's
screenshots are taken from; install_demo_user.py puts the same persona in
the real database.

    python tools/demo/build_demo.py           # build (or rebuild) the demo
    python tools/demo/build_demo.py --offline # no Yahoo Finance: synthetic prices
"""
from __future__ import annotations

import argparse
import shutil

import _sandbox  # noqa: F401  (must run before any app import)
from _sandbox import DATA_DIR, DEMO_HOME, DEMO_PASSWORD, DEMO_USER, STATEMENTS_DIR, assert_isolated

from app.db import database  # noqa: E402
from demo_data import populate  # noqa: E402


def _wipe() -> None:
    from sqlalchemy.orm import close_all_sessions
    close_all_sessions()
    database.engine.dispose()
    for f in list(DATA_DIR.glob("profin*.db*")) + list(STATEMENTS_DIR.glob("*.xlsx")):
        f.unlink()
    for folder in (DATA_DIR / "backups", DEMO_HOME / "FAMILYFIN" / "backups"):
        if folder.exists():
            shutil.rmtree(folder)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="no Yahoo Finance: synthetic prices")
    args = parser.parse_args()

    assert_isolated()
    _wipe()
    database.init_db()
    assert_isolated()
    print(f"Construyendo la DEMO en {DEMO_HOME}")

    session = database.SessionLocal()
    try:
        populate(session, DEMO_USER, DEMO_PASSWORD, STATEMENTS_DIR, offline=args.offline)
    finally:
        session.close()
    assert_isolated()
    print(f"\nDEMO lista. Usuario: {DEMO_USER}   Contraseña: {DEMO_PASSWORD}")
    print("Arranque: python tools/demo/run_demo.py   (o tools/demo/PROFIN_DEMO.bat)")


if __name__ == "__main__":
    main()
