"""
tests/test_investments_import_myinvestor.py
===========================================
MyInvestor account-movements import: kind guessing, product grouping,
quantity estimation from prices, the import itself and the preview dialog.
The CSV is synthetic (made-up amounts) in MyInvestor's layout.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QElapsedTimer
from PySide6.QtWidgets import QApplication
from sqlalchemy.orm import Session

from app.config import THEMES
from app.db.database import _build_engine
from app.db.models import AssetType, Base, InvestmentKind, InvestmentTransaction, Security, User
from app.investments.importers.base import KIND_BUY, KIND_DIVIDEND, KIND_IGNORE, KIND_SELL
from app.investments.importers.myinvestor import estimate_quantities, looks_like_myinvestor, parse_statement
from app.investments.importers.service import apply_import, fetch_security_infos, parse_any, symbols_to_fetch
from app.investments.importers.symbols import search_queries, suggest_symbols
from app.investments.market_data import SearchResult, SecurityInfo

HEADER = "Fecha de operación;Fecha de valor;Concepto;Importe;Divisa"
# Newest first, like the export.
LINES = [
    "15/09/2026;16/09/2026;GLOBAL EQUITY INDEX FUND P AC;-300,00;EUR",
    "10/09/2026;09/09/2026;PERIODO 09/08/2026 09/09/2026;0,11;EUR",
    "23/07/2026;24/07/2026;LABORATORIOS EJEMPLO SA;396,50;EUR",
    "15/07/2026;15/07/2026;LABORATORIOS EJEMPL SA;4,20;EUR",
    "02/06/2026;03/06/2026;LABORATORIOS EJEMPLO SA;-402,00;EUR",
    "02/06/2026;02/06/2026;ana;500;EUR",
    "24/03/2026;24/03/2026;Transferencia desde MyInvestor;-100;EUR",
    "23/03/2026;25/03/2026;WORLD DEVELOPED INDEX FUND;104,00;EUR",
    "20/02/2026;24/02/2026;WORLD DEVL INDX FUND D EUR @;-100,00;EUR",
    "11/02/2026;13/02/2026;ACME CORP @ 5;560,00;EUR",
    "30/12/2025;29/12/2025;ACME CORP @ 5;0,05;EUR",
    "12/08/2025;14/08/2025;ACME CORP @ 5;-500,00;EUR",
    "11/08/2025;11/08/2025;ana;1700;EUR",
    "09/08/2025;09/08/2025;;0;EUR",
]


def write_csv(path, lines=LINES):
    path.write_text("\r\n".join([HEADER] + lines) + "\r\n", encoding="utf-8")
    return path


@pytest.fixture
def statement(tmp_path):
    return parse_statement(write_csv(tmp_path / "mi.csv"))


def by(statement, name_start):
    return [r for r in sorted(statement.rows, key=lambda r: r.day) if r.name.startswith(name_start)]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_format_is_detected(tmp_path):
    path = write_csv(tmp_path / "mi.csv")
    assert looks_like_myinvestor(path)
    assert parse_any(path).broker == "MyInvestor"


def test_kinds_are_guessed_from_sign_and_size(statement):
    acme = by(statement, "ACME")
    assert [(r.kind, r.quantity, r.amount_eur) for r in acme] == [
        (KIND_BUY, 5, 500.0), (KIND_DIVIDEND, None, 0.05), (KIND_SELL, 5, 560.0)]
    assert acme[0].price == 100.0 and not acme[0].quantity_estimated
    labs = by(statement, "LABORATORIOS")
    assert [r.kind for r in labs] == [KIND_BUY, KIND_DIVIDEND, KIND_SELL]
    assert labs[0].quantity is None and labs[0].quantity_estimated
    assert all(r.kind_editable for r in statement.rows)


def test_truncated_spellings_share_one_product(statement):
    labs = {r.key for r in by(statement, "LABORATORIOS")}
    fund = {r.key for r in statement.rows if "WORLD" in r.name}
    assert len(labs) == 1 and len(fund) == 1
    product = statement.products[fund.pop()]
    assert product["fund"] and product["spellings"] == ["WORLD DEVELOPED INDEX FUND",
                                                        "WORLD DEVL INDX FUND D EUR"]
    sale = [r for r in statement.rows if r.name == "WORLD DEVELOPED INDEX FUND"][0]
    assert sale.kind == KIND_SELL                      # matched with the purchase


def test_money_movements_and_interest_are_ignored(statement):
    assert statement.ignored == {"Movimientos sin importe": 1, "Intereses de la cuenta": 1,
                                 "Ingresos y transferencias de dinero": 3}
    assert len(statement.rows) == 9
    assert (statement.first_day, statement.last_day) == (date(2025, 8, 9), date(2026, 9, 15))


def test_refs_are_stable_and_unique(tmp_path):
    lines = LINES + ["12/08/2025;14/08/2025;ACME CORP @ 5;-500,00;EUR"]      # identical row
    s1 = parse_statement(write_csv(tmp_path / "a.csv", lines))
    s2 = parse_statement(write_csv(tmp_path / "b.csv", lines))
    refs = [r.ref for r in s1.rows]
    assert len(refs) == len(set(refs)) and refs == [r.ref for r in s2.rows]


def test_credit_without_previous_purchase_is_ignored_for_review(tmp_path):
    s = parse_statement(write_csv(tmp_path / "a.csv", ["01/02/2026;01/02/2026;SOMETHING SA;50,00;EUR"]))
    assert s.rows[0].kind == KIND_IGNORE and "revisa" in s.rows[0].note


# ---------------------------------------------------------------------------
# Quantity estimation
# ---------------------------------------------------------------------------

def _closes(start, end, value):
    return [(start + timedelta(days=i), value) for i in range((end - start).days + 1)]


def test_quantities_are_estimated_rounded_and_sales_close_the_position(statement):
    labs_key = by(statement, "LABORATORIOS")[0].key
    fund_key = [r.key for r in statement.rows if "WORLD" in r.name][0]
    japan_key = [r.key for r in statement.rows if r.name.startswith("GLOBAL")][0]
    symbols = {labs_key: "LAB.MC", fund_key: "0PWORLD.F", japan_key: "0PGLOBAL.F"}
    closes = {
        "LAB.MC": _closes(date(2026, 5, 20), date(2026, 7, 31), 66.4),    # 402/66.4 = 6.05 → 6
        "0PWORLD.F": _closes(date(2026, 2, 1), date(2026, 3, 31), 25.0),  # 100/25 = 4 ; 104/25 = 4.16 → all
        "0PGLOBAL.F": _closes(date(2026, 9, 1), date(2026, 9, 20), 12.0),
    }
    estimate_quantities(statement.rows, symbols, closes,
                        {k: p["fund"] for k, p in statement.products.items()})
    buy, _div, sale = by(statement, "LABORATORIOS")
    assert (buy.quantity, sale.quantity) == (6.0, 6.0)
    assert buy.price == pytest.approx(67.0)
    fund_rows = [r for r in statement.rows if r.key == fund_key]
    assert [r.quantity for r in fund_rows] == [4.0, 4.0]
    japan = [r for r in statement.rows if r.key == japan_key][0]
    assert japan.quantity == pytest.approx(25.0)


def test_missing_price_leaves_quantity_empty(statement):
    estimate_quantities(statement.rows, {}, {}, {})
    assert by(statement, "LABORATORIOS")[0].quantity is None


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

class FakeProvider:
    name = "fake"

    def search(self, text, limit=10):
        table = {
            "ACME CORP": [SearchResult("ACME", "Acme Corp", AssetType.stock, "NASDAQ"),
                          SearchResult("ACM.DE", "Acme Corp", AssetType.stock, "XETRA")],
            "LABORATORIOS EJEMPLO SA": [
                SearchResult("LAB.MU", "Laboratorios Ejemplo SA", AssetType.stock, "Munich"),
                SearchResult("LAB.MC", "Laboratorios Ejemplo, S.A.", AssetType.stock, "Madrid")],
            "WORLD DEVL INDX FUND D EUR": [],
            "WORLD DEV WLD IDX FUND": [],
            "WORLD DEVL INDX FUND": [
                SearchResult("0PWORLDH.F", "World Dev Idx Fund D Acc EUR H", AssetType.fund, "Frankfurt"),
                SearchResult("0PWORLD.F", "World Dev Idx Fund D Acc EUR", AssetType.fund, "Frankfurt"),
                SearchResult("0PWORLDU", "World Dev Idx Fund D Acc USD", AssetType.fund, "Other"),
                SearchResult("WLDETF.DE", "World ETF", AssetType.etf, "XETRA")],
            "GLOBAL EQUITY INDEX FUND": [
                SearchResult("0PGLOBAL.F", "Global Equity Index Fund EUR P Acc", AssetType.fund, "Frankfurt")],
        }
        return table.get(text, [])

    def daily_closes(self, symbols, start, end=None):
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        prices = {"ACM.DE": 100.5, "ACME": 150.0, "LAB.MC": 67.0, "LAB.MU": 66.8,
                  "0PWORLD.F": 25.0, "0PGLOBAL.F": 12.0}
        return {s: [(d, prices[s]) for d in days] for s in symbols if s in prices}

    def security_info(self, symbol):
        fund = symbol.startswith("0P")
        return SecurityInfo(symbol=symbol, name=f"{symbol} name",
                            asset_type=AssetType.fund if fund else AssetType.stock,
                            currency="EUR", exchange="X")


def test_fund_queries_expand_abbreviations():
    queries = search_queries("NAME:X", {"name": "ISHARES DEVL WRLD INDX D EUR", "fund": True,
                                        "spellings": ["ISHARES DEVL WRLD INDX D EUR"]})
    assert "ISHARES DEV WLD IDX" in queries and "NAME:X" not in queries


def test_suggestions_by_price_by_venue_and_by_fund_class(statement):
    choices = suggest_symbols(FakeProvider(), statement.products)
    by_name = {c.name: c for c in choices.values()}
    assert by_name["ACME CORP"].chosen == "ACM.DE"                       # price matches (EUR)
    assert by_name["LABORATORIOS EJEMPLO SA"].chosen == "LAB.MC"         # no price: home venue
    fund = next(c for c in choices.values() if c.name.startswith("WORLD"))
    assert fund.chosen == "0PWORLD.F"                                    # EUR, unhedged, a fund
    assert {c.symbol for c in fund.candidates} == {"0PWORLD.F", "0PWORLDH.F"}   # no USD, no ETF


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'mi.db'}")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ana = User(username="ana", password_hash="x")
    s.add(ana)
    s.commit()
    yield SimpleNamespace(s=s, ana=ana, path=tmp_path)
    s.close()
    engine.dispose()


def prepared(db):
    statement = parse_statement(write_csv(db.path / "mi.csv"))
    provider = FakeProvider()
    symbols = {k: c.chosen for k, c in suggest_symbols(provider, statement.products).items()}
    from app.investments.importers.service import estimate_quantities as estimate, fetch_closes
    estimate(statement, symbols, fetch_closes(provider, statement, symbols))
    infos = fetch_security_infos(provider, symbols_to_fetch(db.s, symbols.values()))
    return statement, symbols, infos


def test_import_writes_estimated_operations_and_reimport_skips(db):
    statement, symbols, infos = prepared(db)
    summary = apply_import(db.s, db.ana.id, statement, symbols, infos)
    assert (summary.trades, summary.dividends) == (7, 2)
    labs = db.s.query(Security).filter_by(symbol="LAB.MC").one()
    assert labs.isin is None                                 # no ISIN in MyInvestor files
    lab_ops = (db.s.query(InvestmentTransaction).filter_by(security_id=labs.id)
               .order_by(InvestmentTransaction.trade_date).all())
    assert [(t.kind, t.quantity) for t in lab_ops] == [
        (InvestmentKind.buy, 6.0), (InvestmentKind.dividend, None), (InvestmentKind.sell, 6.0)]
    assert "estimadas" in lab_ops[0].notes and lab_ops[0].broker == "MyInvestor"

    again, symbols, infos = prepared(db)
    assert apply_import(db.s, db.ana.id, again, symbols, infos).result.skipped == 9


def test_rows_marked_ignore_are_not_imported(db):
    statement, symbols, infos = prepared(db)
    for row in statement.rows:
        if row.kind == KIND_DIVIDEND:
            row.kind = KIND_IGNORE
    summary = apply_import(db.s, db.ana.id, statement, symbols, infos)
    assert summary.dividends == 0 and summary.ignored == 2


# ---------------------------------------------------------------------------
# Preview dialog
# ---------------------------------------------------------------------------

def wait_until(condition, timeout_ms=5000):
    app = QApplication.instance()
    timer = QElapsedTimer()
    timer.start()
    while not condition() and timer.elapsed() < timeout_ms:
        app.processEvents()
    return condition()


def test_dialog_estimates_lets_the_user_fix_and_imports(db):
    QApplication.instance() or QApplication([])
    from app.gui.investment_import import COL_KIND, COL_QTY, ImportStatementDialog

    dialog = ImportStatementDialog(db.s, db.ana.id, "ana", FakeProvider(), THEMES["light"])
    dialog.load_file(write_csv(db.path / "mi.csv"))
    assert wait_until(lambda: not dialog.busy and dialog._btn_import.isEnabled())
    rows = dialog._rows
    lab_buy = next(i for i, r in enumerate(rows) if r.name.startswith("LABORATORIOS") and r.kind == KIND_BUY)
    assert dialog._ops.item(lab_buy, COL_QTY).text() == "6"

    # The user corrects the estimate and turns a dividend into "Ignorar".
    dialog._ops.item(lab_buy, COL_QTY).setText("5")
    assert wait_until(lambda: rows[lab_buy].quantity == 5 and not rows[lab_buy].quantity_estimated)
    div = next(i for i, r in enumerate(dialog._rows) if r.kind == KIND_DIVIDEND)
    combo = dialog._ops.cellWidget(div, COL_KIND)
    combo.setCurrentIndex(combo.findData(KIND_IGNORE))
    assert wait_until(lambda: "Ignorar" in [dialog._ops.cellWidget(div, COL_KIND).currentText()])

    # 5 bought, 6 estimated for the sale → the sale must be corrected too, or the
    # import is refused as a whole.
    dialog._import()
    assert wait_until(lambda: not dialog._runner.busy)
    QApplication.instance().processEvents()
    assert dialog.summary is None and "No se ha importado nada" in dialog._lbl_status.text()
    sale = next(i for i, r in enumerate(dialog._rows) if r.name.startswith("LABORATORIOS") and r.kind == KIND_SELL)
    dialog._ops.item(sale, COL_QTY).setText("5")
    assert wait_until(lambda: dialog._rows[sale].quantity == 5)
    dialog._import()
    assert wait_until(lambda: dialog.summary is not None)
    assert dialog.summary.ignored == 1 and dialog.summary.dividends == 1
