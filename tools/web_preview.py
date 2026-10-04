"""
tools/web_preview.py
====================
FAMILYFIN — Developer preview of the web front-end on a DEMO database.

Never touches the family's data: it builds its own SQLite file under
dist/web_preview/ (gitignored) and serves plain HTTP on 127.0.0.1 only.

    python tools/web_preview.py            # http://localhost:8765  (demo / demo-pass)
    python tools/web_preview.py --reset    # rebuild the demo database

Dev-only helpers (this process only, never in the real app):
  * CSS/JS edits are picked up without restarting (asset hash cache cleared
    on every request; templates already auto-reload). Python edits need a
    restart, and a restart logs you out (sessions live in memory).
  * /__frame?w=820&h=1180&src=/movimientos shows a page at an exact device
    width (iPad portrait 820, landscape 1180, iPhone 390) — useful when the
    browser window cannot be resized. For that, this process drops the CSP
    and X-Frame-Options headers.
  * /__demo-extracto.xlsx serves a small CaixaBank-style statement to test
    /importar in a browser (attach it to the file input with a DataTransfer,
    see docs/web_parity_plan.md → "Cómo probar").
"""
from __future__ import annotations

import argparse
import html
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.auth.auth_service import create_user  # noqa: E402
from app.categorization.category_manager import seed_default_categories, seed_default_rules  # noqa: E402
from app.categorization.rules_engine import apply_rules_and_commit  # noqa: E402
from app.db.models import (  # noqa: E402
    Account, AssetType, Base, InvestmentKind, InvestmentTransaction, Security, Transaction, TransactionType,
)
from app.investments.prices import set_manual_price  # noqa: E402
from app.web import app as web_app_module  # noqa: E402
from app.web.app import create_app  # noqa: E402
from app.web.deps import get_db  # noqa: E402
from app.web.security import LoginLimiter, SessionStore  # noqa: E402

HERE = ROOT / "dist" / "web_preview"
DB = HERE / "preview.db"
STATEMENT = HERE / "extracto_demo.xlsx"
USER, PASSWORD = "demo", "demo-pass"


def _seed(Session) -> None:
    random.seed(7)
    db = Session()
    user = create_user(db, USER, PASSWORD)
    seed_default_categories(db, user.id)
    seed_default_rules(db, user.id)
    acc = Account(user_id=user.id, account_name="CaixaBank Principal", bank="CaixaBank", iban_last4="4821")
    db.add(acc)
    db.flush()
    merchants = [("MERCADONA", -60, -140), ("IBERDROLA CLIENTES", -45, -95), ("VODAFONE ESPANA", -35, -55),
                 ("AMAZON EU SARL", -12, -120), ("REPSOL ESTACION", -40, -75), ("NETFLIX.COM", -13, -13),
                 ("SPOTIFY", -11, -11), ("GLOVO APP", -18, -35), ("RESTAURANTE EL PUERTO", -25, -80),
                 ("FARMACIA CENTRAL", -8, -30), ("TELPARK", -3, -12), ("BAZAR CHINO", -5, -20)]
    balance, day, rows = 8400.0, date.today() - timedelta(days=200), []
    while day <= date.today():
        if day.day == 28:
            rows.append((day, "PAGE GROUP SPAIN RECURSOS HUMANOS", 2850.0))
        for _ in range(random.choice([0, 1, 1, 2])):
            name, lo, hi = random.choice(merchants)
            rows.append((day, name, round(random.uniform(hi, lo), 2)))
        day += timedelta(days=1)
    for d, name, amount in rows:
        balance += amount
        db.add(Transaction(user_id=user.id, account_id=acc.id, transaction_date=d, amount=amount,
                           running_balance=round(balance, 2), concept=name, issuer_name=name,
                           type=TransactionType.income if amount > 0 else TransactionType.expense))
    db.commit()
    apply_rules_and_commit(db, [t.id for t in db.query(Transaction).filter(Transaction.user_id == user.id)], user.id)

    # A small portfolio with manual (offline) prices, for the Inversiones pages.
    start = date.today() - timedelta(days=180)
    for symbol, name, kind, qty, price in [("DEMO.MC", "Demo Energía SA", AssetType.stock, 40, 12.5),
                                           ("WRLD.DE", "Demo World ETF", AssetType.etf, 25, 80.0)]:
        sec = Security(symbol=symbol, name=name, asset_type=kind, currency="EUR")
        db.add(sec)
        db.flush()
        db.add(InvestmentTransaction(user_id=user.id, security_id=sec.id, broker="DEGIRO",
                                     kind=InvestmentKind.buy, trade_date=start, quantity=qty, price=price,
                                     currency="EUR", amount_eur=round(qty * price + 2, 2), fees_eur=2))
        db.commit()
        close = price
        for i in range(0, 181, 3):
            close = round(close * random.uniform(0.985, 1.02), 2)
            set_manual_price(db, sec.id, start + timedelta(days=i), close)
    db.close()


def _statement() -> None:
    from tests.test_import_pipeline import _row, _write_export   # same layout the importer tests use

    _write_export(STATEMENT, [
        _row("01/10/2026", gasto=58.2, saldo_pos=19331.61, issuer="MERCADONA VALENCIA", details=("COMPRA TARJETA",)),
        _row("02/10/2026", gasto=34.9, saldo_pos=19296.71, issuer="VODAFONE ESPANA", details=("RECIBO",)),
        _row("03/10/2026", ingreso=120.0, saldo_pos=19416.71, comun="012", issuer="BIZUM DE LUCIA", details=("CENA",)),
        _row("03/10/2026", gasto=0.0, saldo_pos=19416.71, comun="099", issuer="AJUSTE"),
        _row("04/10/2026", gasto=19.99, saldo_pos=19396.72, issuer="SPOTIFY AB", details=("SUSCRIPCION",)),
    ])


def build_app(reset: bool = False):
    HERE.mkdir(parents=True, exist_ok=True)
    if reset and DB.exists():
        DB.unlink()
    fresh = not DB.exists()
    engine = create_engine(f"sqlite:///{DB}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys = ON"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    if fresh:
        _seed(Session)
    if not STATEMENT.exists():
        _statement()

    def demo_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app(sessions=SessionStore(), limiter=LoginLimiter(), on_change=lambda *a: None, lan_only=True)
    app.dependency_overrides[get_db] = demo_db

    @app.middleware("http")
    async def dev_helpers(request, call_next):
        from starlette.responses import HTMLResponse, Response

        web_app_module.asset.cache_clear()
        if request.url.path == "/__demo-extracto.xlsx":
            return Response(STATEMENT.read_bytes(), media_type="application/octet-stream")
        if request.url.path == "/__frame":
            w = int(request.query_params.get("w", 820))
            h = int(request.query_params.get("h", 1180))
            src = html.escape(request.query_params.get("src", "/"), quote=True)
            return HTMLResponse(f'<body style="margin:0;background:#333"><iframe src="{src}" '
                                f'style="width:{w}px;height:{h}px;border:0;background:#fff"></iframe></body>')
        response = await call_next(request)
        for header in ("content-security-policy", "x-frame-options"):
            if header in response.headers:
                del response.headers[header]
        return response

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="rebuild the demo database")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = build_app(args.reset)
    print(f"FAMILYFIN web preview (DEMO data): http://localhost:{args.port}  user {USER} / {PASSWORD}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
