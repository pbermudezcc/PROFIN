"""
tools/demo/install_demo_user.py
===============================
FAMILYFIN — Adds the DEMO user (DEMO / 123456) to the family's REAL
database, to show the app to visitors from the PC or the web.

The persona and its data come from demo_data.py (all fictitious). The
family's data is not modified:
  * a snapshot of profin.db is written next to it first
    (profin.before-demo-user.<timestamp>.db);
  * DEMO never sees nor gets applied the family's global rules
    (rules_engine.rule_scope), so their patterns are not shown to visitors;
  * securities are shared: existing ones are reused untouched.
Close FAMILYFIN before running it.

    python tools/demo/install_demo_user.py             # create DEMO
    python tools/demo/install_demo_user.py --replace   # rebuild an existing DEMO
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import DEMO_USERNAME  # noqa: E402
from app.db import database  # noqa: E402
from app.db.models import Account, AutoCategorizationRule, Transaction, User  # noqa: E402

DEMO_PASSWORD = "123456"
STATEMENTS_DIR = ROOT / "dist" / "demo" / "extractos_demo_real"


def _app_running() -> bool:
    """FAMILYFIN.exe, or `python main.py` from this checkout."""
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'FAMILYFIN*' -or "
              "($_.Name -like 'python*' -and $_.CommandLine -like '*main.py*') } | "
              "Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(out.strip())


def _counts(session, user_id: int) -> tuple[int, int]:
    return (session.query(Transaction).filter(Transaction.user_id == user_id).count(),
            session.query(AutoCategorizationRule).filter(AutoCategorizationRule.user_id == user_id).count())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--replace", action="store_true", help="delete and rebuild an existing DEMO user")
    parser.add_argument("--offline", action="store_true", help="no Yahoo Finance: synthetic prices")
    parser.add_argument("--force", action="store_true", help="skip the 'FAMILYFIN is running' check")
    args = parser.parse_args()

    if not args.force and _app_running():
        sys.exit("FAMILYFIN está abierto. Ciérralo (también la web) y vuelve a ejecutar este script.")

    database.init_db()
    db_path = Path(str(database.DB_PATH))
    print(f"Base de datos: {db_path}")

    session = database.SessionLocal()
    try:
        existing = session.query(User).filter(User.username == DEMO_USERNAME).one_or_none()
        if existing is not None and not args.replace:
            sys.exit(f"El usuario {DEMO_USERNAME} ya existe. Usa --replace para regenerarlo.")

        others = {u.username: _counts(session, u.id) for u in session.query(User)
                  if u.username != DEMO_USERNAME}
        globals_before = session.query(AutoCategorizationRule).filter(
            AutoCategorizationRule.user_id.is_(None)).count()

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        snapshot = db_path.with_name(f"profin.before-demo-user.{stamp}.db")
        database.snapshot_database(db_path, snapshot)
        print(f"Copia previa: {snapshot}")

        if existing is not None:
            # Account.user_id is SET NULL on delete: remove the accounts (and
            # their transactions) explicitly; the rest cascades from the user.
            for account in session.query(Account).filter(Account.user_id == existing.id):
                session.delete(account)
            session.delete(existing)
            session.commit()
            print(f"Usuario {DEMO_USERNAME} anterior eliminado.")

        from demo_data import populate
        user = populate(session, DEMO_USERNAME, DEMO_PASSWORD, STATEMENTS_DIR, offline=args.offline)

        # The family's data must be exactly as it was.
        after = {u.username: _counts(session, u.id) for u in session.query(User)
                 if u.username != DEMO_USERNAME}
        globals_after = session.query(AutoCategorizationRule).filter(
            AutoCategorizationRule.user_id.is_(None)).count()
        if after != others or globals_after != globals_before:
            sys.exit(f"¡Atención! Los datos de la familia han cambiado: {others} → {after}. "
                     f"Restaura la copia {snapshot}.")

        from app.categorization.rules_engine import get_all_rules
        leaked = [r for r in get_all_rules(session, user_id=user.id) if r.user_id is None]
        if leaked:
            sys.exit("¡Atención! DEMO ve reglas globales de la familia.")
    finally:
        session.close()

    print(f"\nUsuario {DEMO_USERNAME} listo (contraseña {DEMO_PASSWORD}). Datos de la familia intactos: "
          + ", ".join(f"{name} {tx} movimientos" for name, (tx, _) in sorted(others.items())))
    print(f"Extracto de octubre para importar en directo: {STATEMENTS_DIR}")


if __name__ == "__main__":
    main()
