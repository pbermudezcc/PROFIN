"""
tests/test_investments_import.py
================================
DEGIRO account statement import: parsing (.xlsx and .csv), symbol
suggestion, and the all-or-nothing import with duplicate detection.
The statement is synthetic (made-up amounts) but follows DEGIRO's layout.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.db.database import _build_engine
from app.db.models import AssetType, Base, InvestmentKind, InvestmentTransaction, Security, User
from app.investments.importers.degiro import StatementError, parse_statement
from app.investments.importers.service import (
    apply_import,
    catalogue_isins,
    fetch_security_infos,
    symbols_to_fetch,
)
from app.investments.importers.symbols import search_queries, suggest_symbols
from app.investments.ledger import TransactionDraft, save_transaction
from app.investments.market_data import SearchResult, SecurityInfo
from app.investments.portfolio import LedgerError

HEADER = ["Fecha", "Hora", "Fecha valor", "Producto", "ISIN", "Descripción", "Tipo",
          "Variación", None, "Saldo", None, "ID Orden"]
ACME, ETF, SIE, USCO = "US0000000001", "IE0000000002", "DE0000000003", "US0000000004"

# Oldest first here; the file is written newest first like DEGIRO does.
ROWS = [
    ("01-08-2025", "09:00", None, None, "flatex Deposit", None, "EUR", 5000),
    ("12-08-2025", "10:00", "ACME CORP", ACME, "Compra 10 Acme Corp.@100 EUR (US0000000001)", None, "EUR", -1000, "o1"),
    ("12-08-2025", "10:00", "ACME CORP", ACME, "Costes de transacción y/o externos de DEGIRO", None, "EUR", -3.9, "o1"),
    # one order filled twice
    ("13-08-2025", "16:04", "WORLD ETF", ETF, "Compra 17 World ETF USD Dis@50 EUR (IE0000000002)", None, "EUR", -850, "o2"),
    ("13-08-2025", "16:04", "WORLD ETF", ETF, "Costes de transacción y/o externos de DEGIRO", None, "EUR", -1, "o2"),
    ("13-08-2025", "16:11", "WORLD ETF", ETF, "Compra 1 World ETF USD Dis@50 EUR (IE0000000002)", None, "EUR", -50, "o2"),
    ("15-08-2025", "12:00", "SIEMENS X", SIE, "Compra 5 Siemens X AG@20 EUR (DE0000000003)", None, "EUR", -100, "o3"),
    ("15-08-2025", "12:00", "SIEMENS X", SIE, "Spanish Transaction Tax", None, "EUR", -0.2, "o3"),
    ("15-08-2025", "12:00", "SIEMENS X", SIE, "Costes de transacción y/o externos de DEGIRO", None, "EUR", -2, "o3"),
    # a USD trade converted with AutoFX (conversion rows carry the order id)
    ("20-08-2025", "15:30", "US CO", USCO, "Compra 2 US Co Inc@110 USD (US0000000004)", None, "USD", -220, "o4"),
    ("20-08-2025", "15:30", None, None, "Retirada Cambio de Divisa", None, "EUR", -200, "o4"),
    ("20-08-2025", "15:30", None, None, "Ingreso Cambio de Divisa", 1.1, "USD", 220, "o4"),
    ("20-08-2025", "15:30", "US CO", USCO, "Costes de transacción y/o externos de DEGIRO", None, "EUR", -1, "o4"),
    # USD dividends: tax booked minutes apart, converted the next morning together
    ("01-10-2025", "08:37", "ACME CORP", ACME, "Retención del dividendo", None, "USD", -0.6, None),
    ("01-10-2025", "08:41", "ACME CORP", ACME, "Dividendo", None, "USD", 4.0, None),
    ("01-10-2025", "09:00", "WORLD ETF", ETF, "Dividendo", None, "USD", 6.6, None),
    ("02-10-2025", "06:50", None, None, "Retirada Cambio de Divisa", 1.2, "USD", -10.0, None),
    ("02-10-2025", "06:50", None, None, "Ingreso Cambio de Divisa", None, "EUR", 8.33, None),
    # EUR dividend: no conversion
    ("03-10-2025", "08:00", "SIEMENS X", SIE, "Dividendo", None, "EUR", 1.5, None),
    ("03-10-2025", "08:00", "SIEMENS X", SIE, "Retención del dividendo", None, "EUR", -0.4, None),
    ("05-01-2026", "10:00", None, None, "Comisión de conectividad con el mercado 2026 (Xetra - XET)", None, "EUR", -2.5, None),
    ("06-01-2026", "10:00", None, None, "Degiro Cash Sweep Transfer", None, "EUR", 12.0, None),
    ("03-03-2026", "15:30", "WORLD ETF", ETF, "Venta 18 World ETF USD Dis@55 EUR (IE0000000002)", None, "EUR", 990, "o5"),
    ("03-03-2026", "15:30", "WORLD ETF", ETF, "Costes de transacción y/o externos de DEGIRO", None, "EUR", -3, "o5"),
]


def _as_values(row):
    day, clock, product, isin, desc, rate, currency, amount, *rest = row
    return [day, clock, day, product, isin, desc, rate, currency, amount, "EUR", 0, rest[0] if rest else None]


def write_xlsx(path, rows=ROWS):
    wb = Workbook()
    ws = wb.active
    ws.title = "Estado de cuenta"
    ws.append(HEADER)
    for row in reversed(rows):
        ws.append(_as_values(row))
    wb.save(path)
    return path


def write_csv(path, rows=ROWS):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["" if h is None else h for h in HEADER])
        for row in reversed(rows):
            values = _as_values(row)
            # DEGIRO's CSV uses decimal commas for amounts and rates
            values = ["" if v is None else (f"{v:.2f}".replace(".", ",") if isinstance(v, (int, float))
                                            and i in (6, 8, 10) else v) for i, v in enumerate(values)]
            writer.writerow(values)
    return path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.fixture(params=["xlsx", "csv"])
def statement(request, tmp_path):
    writer = write_xlsx if request.param == "xlsx" else write_csv
    return parse_statement(writer(tmp_path / f"Account.{request.param}"))


def test_trades_merge_fills_and_add_their_costs(statement):
    trades = {t.ref: t for t in statement.trades}
    assert set(trades) == {"o1", "o2", "o3", "o4", "o5"}
    acme = trades["o1"]
    assert (acme.kind, acme.trade_date, acme.quantity, acme.price, acme.amount_eur, acme.fees_eur) == (
        "buy", date(2025, 8, 12), 10, 100, 1003.9, 3.9)
    etf = trades["o2"]
    assert (etf.quantity, etf.fills, etf.price, etf.amount_eur) == (18, 2, 50, 901.0)
    assert trades["o3"].fees_eur == pytest.approx(2.2)            # DEGIRO fee + Spanish tax
    sale = trades["o5"]
    assert (sale.kind, sale.quantity, sale.amount_eur) == ("sell", 18, 987.0)


def test_foreign_currency_trade_uses_the_autofx_euros(statement):
    usco = next(t for t in statement.trades if t.ref == "o4")
    assert (usco.currency, usco.price, usco.amount_eur, usco.fees_eur) == ("USD", 110, 201.0, 1.0)


def test_dividends_take_the_euros_actually_credited(statement):
    divs = {d.isin: d for d in statement.dividends}
    acme, etf, sie = divs[ACME], divs[ETF], divs[SIE]
    assert (acme.gross, acme.withheld) == (pytest.approx(4.0), pytest.approx(0.6))
    # 10 USD converted into 8.33 EUR, shared 3.4 : 6.6
    assert acme.net_eur == pytest.approx(2.83) and etf.net_eur == pytest.approx(5.50)
    assert acme.withheld_eur == pytest.approx(0.5) and acme.rate == 1.2
    assert (sie.net_eur, sie.withheld_eur) == (pytest.approx(1.1), pytest.approx(0.4))
    assert acme.ref == f"DIV|{ACME}|2025-10-01"


def test_everything_else_is_ignored_and_listed(statement):
    assert set(statement.ignored) == {"flatex Deposit", "Comisión de conectividad con el mercado 2026",
                                      "Degiro Cash Sweep Transfer"}
    assert statement.warnings == []
    assert (statement.first_day, statement.last_day) == (date(2025, 8, 1), date(2026, 3, 3))


def test_dividend_without_conversion_is_warned(tmp_path):
    rows = [r for r in ROWS if not (r[0] == "02-10-2025")]
    statement = parse_statement(write_xlsx(tmp_path / "a.xlsx", rows))
    acme = next(d for d in statement.dividends if d.isin == ACME)
    assert acme.net_eur is None
    assert any("no se encontró su cambio" in w for w in statement.warnings)


def test_xlsx_with_a_wrong_dimension_is_read_whole(tmp_path):
    # Real DEGIRO export (Oct 2026): <dimension ref="A1"/> although the sheet
    # has 12 columns and ~120 rows; read-only openpyxl used to see one cell.
    import re
    import zipfile

    good = write_xlsx(tmp_path / "good.xlsx")
    bad = tmp_path / "bad.xlsx"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = re.sub(rb'<dimension ref="[^"]*"/>', b'<dimension ref="A1"/>', data)
                assert b'<dimension ref="A1"/>' in data
            dst.writestr(info, data)
    statement = parse_statement(bad)
    assert len(statement.trades) == 5 and len(statement.dividends) == 3


def test_other_files_are_rejected(tmp_path):
    wb = Workbook()
    wb.active.append(["Concepto", "Importe"])
    wb.save(tmp_path / "otro.xlsx")
    with pytest.raises(StatementError, match="Estado de cuenta"):
        parse_statement(tmp_path / "otro.xlsx")
    (tmp_path / "x.pdf").write_bytes(b"%PDF")
    with pytest.raises(StatementError, match="Formato"):
        parse_statement(tmp_path / "x.pdf")


# ---------------------------------------------------------------------------
# Symbol suggestion
# ---------------------------------------------------------------------------

class FakeProvider:
    name = "fake"

    def __init__(self):
        self.searches = []

    def search(self, text, limit=10):
        self.searches.append(text)
        table = {
            ACME: [SearchResult("ACME", "Acme Corp.", AssetType.stock, "NASDAQ")],
            "Acme Corp.": [SearchResult("ACM.F", "Acme", AssetType.stock, "Frankfurt"),
                           SearchResult("ACM.DE", "Acme", AssetType.stock, "XETRA")],
            "WORLD ETF": [SearchResult("WLDA.DE", "World Acc", AssetType.etf, "XETRA"),
                          SearchResult("WLD.AS", "World Dis", AssetType.etf, "Amsterdam")],
        }
        return table.get(text, [])

    def daily_closes(self, symbols, start, end=None):
        data = {"ACM.F": 99.0, "ACM.DE": 99.6, "WLDA.DE": 56.0, "WLD.AS": 50.2}
        end = end or date.today()                    # a price refresh asks up to today
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return {s: [(d, data[s]) for d in days] for s in symbols if s in data}

    def security_info(self, symbol):
        return SecurityInfo(symbol=symbol, name=f"{symbol} name", asset_type=AssetType.stock,
                            currency="EUR", exchange="XETRA")


def test_search_queries_strip_legal_suffixes():
    queries = search_queries(ACME, {"name": "Take-Two Interactive Software, Inc.",
                                    "product": "TAKE-TWO INTERACTIVE SOFTWAR"})
    assert queries[:3] == [ACME, "TAKE-TWO INTERACTIVE SOFTWAR", "Take-Two Interactive Software, Inc."]
    assert "Take-Two Interactive Software" in queries


def test_suggest_prefers_matching_price_then_main_venue_of_same_ticker(tmp_path):
    statement = parse_statement(write_xlsx(tmp_path / "a.xlsx"))
    products = {k: v for k, v in statement.products.items() if k in (ACME, ETF)}
    choices = suggest_symbols(FakeProvider(), products)
    assert choices[ACME].chosen == "ACM.DE"        # both within a point: Xetra over Frankfurt
    assert choices[ETF].chosen == "WLD.AS"         # the other ticker is a different share class
    assert [c.symbol for c in choices[ETF].candidates] == ["WLD.AS", "WLDA.DE"]


def test_catalogued_isin_needs_no_search(tmp_path):
    statement = parse_statement(write_xlsx(tmp_path / "a.xlsx"))
    provider = FakeProvider()
    choices = suggest_symbols(provider, {ACME: statement.products[ACME]}, catalogue={ACME: "ACM.DE"})
    assert choices[ACME].chosen == "ACM.DE" and choices[ACME].from_catalogue
    assert provider.searches == []


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

SYMBOLS = {ACME: "ACM.DE", ETF: "WLD.AS", SIE: "SIX.DE", USCO: "USC"}


@pytest.fixture
def db(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'imp.db'}")
    Base.metadata.create_all(engine)
    s = Session(engine)
    pedro = User(username="pedro", password_hash="x")
    maria = User(username="maria", password_hash="x")
    s.add_all([pedro, maria])
    s.commit()
    yield SimpleNamespace(s=s, pedro=pedro, maria=maria, path=tmp_path)
    s.close()
    engine.dispose()


def run_import(db, user, path):
    statement = parse_statement(path)
    infos = fetch_security_infos(FakeProvider(), symbols_to_fetch(db.s, SYMBOLS.values()))
    return apply_import(db.s, user.id, statement, SYMBOLS, infos)


def test_import_creates_securities_and_operations(db):
    summary = run_import(db, db.pedro, write_xlsx(db.path / "a.xlsx"))
    assert (summary.trades, summary.dividends, summary.result.skipped) == (5, 3, 0)
    assert catalogue_isins(db.s)[ACME] == "ACM.DE"
    etf_buy = (db.s.query(InvestmentTransaction).filter_by(broker_ref="o2").one())
    assert (etf_buy.quantity, etf_buy.amount_eur, etf_buy.broker) == (18, 901.0, "DEGIRO")
    assert "2 ejecuciones" in etf_buy.notes
    div = db.s.query(InvestmentTransaction).filter_by(broker_ref=f"DIV|{ACME}|2025-10-01").one()
    assert (div.kind, div.amount_eur, div.tax_withheld_eur) == (InvestmentKind.dividend, 2.83, 0.5)


def test_reimport_skips_everything_and_other_users_are_independent(db):
    path = write_xlsx(db.path / "a.xlsx")
    run_import(db, db.pedro, path)
    again = run_import(db, db.pedro, path)
    assert again.result.added == [] and again.result.skipped == 8
    maria = run_import(db, db.maria, path)
    assert len(maria.result.added) == 8
    assert db.s.query(InvestmentTransaction).count() == 16


def test_overlapping_statement_imports_only_new_operations(db):
    run_import(db, db.pedro, write_xlsx(db.path / "old.xlsx", ROWS[:-2]))
    summary = run_import(db, db.pedro, write_xlsx(db.path / "new.xlsx"))
    assert [t.broker_ref for t in summary.result.added] == ["o5"]


def test_import_is_all_or_nothing(db):
    # Without the ETF purchase the statement's ETF sale is impossible: the
    # whole import must be rejected, not just that operation.
    rows = [r for r in ROWS if r[-1] != "o2"]
    with pytest.raises(LedgerError, match="solo había 0"):
        run_import(db, db.pedro, write_xlsx(db.path / "bad.xlsx", rows))
    assert db.s.query(InvestmentTransaction).count() == 0
    assert db.s.query(Security).count() == 0          # securities rolled back too


def test_import_respects_existing_manual_operations(db):
    run_import(db, db.pedro, write_xlsx(db.path / "a.xlsx", ROWS[:3]))
    acme = db.s.query(Security).filter_by(symbol="ACM.DE").one()
    save_transaction(db.s, db.pedro.id, TransactionDraft(
        security_id=acme.id, kind=InvestmentKind.sell, trade_date=date(2025, 9, 1),
        broker="DEGIRO", quantity=10, amount_eur=1100.0))
    summary = run_import(db, db.pedro, write_xlsx(db.path / "b.xlsx"))
    assert summary.result.skipped == 1 and len(summary.result.added) == 7
