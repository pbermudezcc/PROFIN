"""
tests/test_investments_gui.py
=============================
Offscreen tests of the Inversiones view and its dialogs: empty state,
entry form (EUR estimate, close-of-day hint, held shares, ledger errors),
editing/deleting from the table, Yahoo search. A fake provider replaces
Yahoo and every database is a temp file.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QElapsedTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from sqlalchemy.orm import sessionmaker

from app.db.database import _build_engine
from app.db.models import (
    AssetType,
    Base,
    FxRate,
    InvestmentKind,
    InvestmentTransaction,
    PriceHistory,
    Security,
    User,
)
from app.gui.investment_dialogs import SecuritySearchDialog, TransactionDialog
from app.gui.investments_view import InvestmentsView
from app.investments.ledger import TransactionDraft, save_transaction
from app.investments.market_data import SearchResult, SecurityInfo
from app.investments.refresh_worker import PriceRefresher
from app.config import THEMES

TODAY = date.today()


class FakeProvider:
    name = "Fake"

    def __init__(self):
        self.closes = {}

    def search(self, text, limit=10):
        return [SearchResult("TKE.DE", "Take-Two Interactive Software, Inc.", AssetType.stock, "XETRA"),
                SearchResult("TTWO", "Take-Two Interactive Software, Inc.", AssetType.stock, "NASDAQ")]

    def security_info(self, symbol):
        return SecurityInfo(symbol=symbol, name="Take-Two Interactive Software, Inc.",
                            asset_type=AssetType.stock, currency="EUR", exchange="XETRA")

    def daily_closes(self, symbols, start, end=None):
        return {s: rows for s, rows in self.closes.items() if s in symbols}


def wait_until(condition, timeout_ms=5000):
    app = QApplication.instance()
    timer = QElapsedTimer()
    timer.start()
    while not condition() and timer.elapsed() < timeout_ms:
        app.processEvents()
    return condition()


@pytest.fixture
def env(tmp_path):
    app = QApplication.instance() or QApplication([])
    engine = _build_engine(f"sqlite:///{tmp_path / 'gui.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    user = User(username="pedro", password_hash="x")
    gev = Security(symbol="GEV", name="GE Vernova Inc.", currency="USD", exchange="NYSE")
    enr = Security(symbol="ENR.DE", name="Siemens Energy AG", currency="EUR", exchange="XETRA")
    session.add_all([user, gev, enr])
    session.flush()
    day = TODAY - timedelta(days=1)
    session.add_all([
        PriceHistory(security_id=gev.id, price_date=day, close=957.63),
        PriceHistory(security_id=enr.id, price_date=day, close=142.6),
        FxRate(currency="USD", rate_date=day, rate=1.25),
    ])
    session.commit()
    provider = FakeProvider()
    ns = SimpleNamespace(app=app, session=session, factory=factory, user=user, gev=gev,
                         enr=enr, provider=provider, theme=THEMES["light"], day=day)
    yield ns
    session.close()
    engine.dispose()


def make_view(env):
    refresher = PriceRefresher(provider=env.provider, session_factory=env.factory)
    return InvestmentsView(env.user, env.session, "light", provider=env.provider,
                           refresher=refresher)


def dialog(env, **kw):
    return TransactionDialog(env.session, env.user.id, env.provider, env.theme, **kw)


def select_security(dlg, security):
    dlg._cmb_security.setCurrentIndex(dlg._cmb_security.findData(security.id))


def buy(env, security, qty, eur, d=None):
    return save_transaction(env.session, env.user.id, TransactionDraft(
        security_id=security.id, kind=InvestmentKind.buy, trade_date=d or env.day,
        broker="DEGIRO", quantity=qty, amount_eur=eur))


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

def test_view_shows_empty_state_then_operations(env):
    view = make_view(env)
    try:
        assert view._ops_stack.currentIndex() == 0
        buy(env, env.enr, 10, 1426.0)
        view.reload()
        assert view._ops_stack.currentIndex() == 1
        assert view._model.rowCount() == 1
        row = [view._model.item(0, c).text() for c in range(view._model.columnCount())]
        assert row[1] == "Compra" and "ENR.DE" in row[2] and row[4] == "10"
        assert row[6] == "−1.426,00 €"
        assert view._lbl_count.text() == "1 operación"
    finally:
        view.shutdown()


def test_view_filters_by_kind_and_text(env):
    buy(env, env.enr, 10, 1426.0)
    buy(env, env.gev, 1, 800.0)
    view = make_view(env)
    try:
        assert view._model.rowCount() == 2
        view._inp_search.setText("siemens")
        assert view._model.rowCount() == 1
        view._inp_search.clear()
        view._cmb_kind.setCurrentIndex(view._cmb_kind.findData(InvestmentKind.sell))
        assert view._model.rowCount() == 0
    finally:
        view.shutdown()


def test_delete_from_view_respects_ledger(env, monkeypatch):
    first = buy(env, env.enr, 10, 1426.0, d=env.day - timedelta(days=5))
    save_transaction(env.session, env.user.id, TransactionDraft(
        security_id=env.enr.id, kind=InvestmentKind.sell, trade_date=env.day,
        broker="DEGIRO", quantity=10, amount_eur=1500.0))
    view = make_view(env)
    warnings = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    try:
        # Select the buy (older row) and try to delete it: the later sale depends on it.
        for row in range(view._proxy.rowCount()):
            if view._proxy.index(row, 1).data() == "Compra":
                view._table.selectRow(row)
        assert view._selected_id() == first.id
        view._delete_selected()
        assert warnings and "No se puede borrar" in warnings[0]
        assert env.session.query(InvestmentTransaction).count() == 2
    finally:
        view.shutdown()


def test_refresh_status_after_refresh(env):
    buy(env, env.enr, 10, 1426.0)
    view = make_view(env)
    try:
        view._refresher.refresh()
        assert wait_until(lambda: not view._refresher.running)
        env.app.processEvents()
        assert "actualizadas" in view._lbl_status.text()
    finally:
        view.shutdown()


def test_theme_toggle_keeps_rows(env):
    buy(env, env.enr, 10, 1426.0)
    view = make_view(env)
    try:
        view.apply_theme("dark")
        assert view._model.rowCount() == 1 and view._theme == "dark"
    finally:
        view.shutdown()


# ---------------------------------------------------------------------------
# Entry dialog
# ---------------------------------------------------------------------------

def test_buy_of_usd_security_estimates_eur_and_saves(env):
    dlg = dialog(env)
    select_security(dlg, env.gev)
    dlg._date.setDate(QDate(env.day.year, env.day.month, env.day.day))
    assert "957,63" in dlg._lbl_close.text()
    dlg._use_close()
    dlg._qty.setValue(2)
    dlg._fees.setValue(2.0)
    # 2 × 957.63 USD / 1.25 + 2 € fees
    assert dlg._amount.value() == pytest.approx(1534.21, abs=0.01)
    assert "1 € = 1,2500 USD" in dlg._lbl_estimate.text()

    dlg._amount.setValue(1530.00)          # exact figure from the statement
    dlg._qty.setValue(2)                   # further edits no longer overwrite it
    assert dlg._amount.value() == 1530.00
    dlg._cmb_broker.setCurrentText("EquatePlus")
    dlg._save()
    saved = env.session.query(InvestmentTransaction).one()
    assert (saved.quantity, saved.price, saved.currency, saved.amount_eur, saved.broker) == (
        2, 957.63, "USD", 1530.0, "EquatePlus")


def test_sell_shows_held_shares_and_rejects_oversell(env):
    buy(env, env.enr, 10, 1426.0, d=env.day - timedelta(days=3))
    dlg = dialog(env, default_kind=InvestmentKind.sell, default_security_id=env.enr.id)
    dlg._date.setDate(QDate(env.day.year, env.day.month, env.day.day))
    assert "Tienes 10" in dlg._lbl_held.text()
    dlg._qty.setValue(11)
    dlg._price.setValue(150.0)
    dlg._save()
    assert dlg._lbl_error.isVisibleTo(dlg)
    assert "solo había 10" in dlg._lbl_error.text()
    assert env.session.query(InvestmentTransaction).count() == 1
    dlg._sell_all()
    assert dlg._qty.value() == 10
    dlg._save()
    assert env.session.query(InvestmentTransaction).count() == 2


def test_dividend_hides_trade_fields_and_saves_withholding(env):
    buy(env, env.enr, 10, 1426.0, d=env.day - timedelta(days=3))
    dlg = dialog(env, default_kind=InvestmentKind.dividend, default_security_id=env.enr.id)
    assert not dlg._qty.isVisibleTo(dlg) and dlg._withheld.isVisibleTo(dlg)
    dlg._amount.setValue(8.5)
    dlg._withheld.setValue(1.5)
    dlg._save()
    div = env.session.query(InvestmentTransaction).filter_by(kind=InvestmentKind.dividend).one()
    assert (div.amount_eur, div.tax_withheld_eur, div.quantity) == (8.5, 1.5, None)


def test_editing_loads_values_and_updates(env):
    txn = buy(env, env.enr, 10, 1426.0)
    dlg = dialog(env, transaction=txn)
    assert dlg._qty.value() == 10 and dlg._amount.value() == 1426.0
    assert not dlg._amount_auto                       # keep the stored exact amount
    dlg._qty.setValue(12)
    assert dlg._amount.value() == 1426.0
    dlg._save()
    env.session.refresh(txn)
    assert txn.quantity == 12
    assert env.session.query(InvestmentTransaction).count() == 1


def test_close_hint_falls_back_to_provider_when_cache_is_missing(env):
    old = date(2024, 3, 8)
    env.provider.closes = {"GEV": [(date(2024, 3, 7), 150.0)],
                           "EURUSD=X": [(date(2024, 3, 7), 1.09)]}
    dlg = dialog(env)
    select_security(dlg, env.gev)
    dlg._date.setDate(QDate(old.year, old.month, old.day))
    assert wait_until(lambda: "150,00" in dlg._lbl_close.text())
    dlg._use_close()
    dlg._qty.setValue(1)
    assert dlg._amount.value() == pytest.approx(round(150.0 / 1.09, 2))
    dlg.reject()


def test_missing_security_is_reported(env):
    dlg = dialog(env)
    dlg._save()
    assert "Selecciona un valor" in dlg._lbl_error.text()


# ---------------------------------------------------------------------------
# Search dialog
# ---------------------------------------------------------------------------

def test_search_dialog_adds_security_to_catalogue(env):
    dlg = SecuritySearchDialog(env.session, env.provider, env.theme, initial_text="take two")
    assert wait_until(lambda: dlg._table.rowCount() == 2)
    assert dlg._table.item(0, 2).text() == "XETRA"
    dlg._inp_isin.setText("us8740541094")
    dlg._add()
    assert wait_until(lambda: dlg.security is not None)
    security = env.session.query(Security).filter_by(symbol="TKE.DE").one()
    assert (security.currency, security.isin) == ("EUR", "US8740541094")


# ---------------------------------------------------------------------------
# Positions tab
# ---------------------------------------------------------------------------

def test_positions_tab_shows_cards_and_rows(env):
    buy(env, env.enr, 10, 1000.0, d=env.day - timedelta(days=40))
    buy(env, env.gev, 2, 1200.0, d=env.day - timedelta(days=40))
    view = make_view(env)
    try:
        assert view._tabs.currentIndex() == 0          # has operations → Posiciones
        panel = view._positions
        assert panel._model.rowCount() == 2
        top = [panel._proxy.index(0, c).data() for c in range(panel._proxy.columnCount())]
        assert "GEV" in top[0]                          # sorted by value: 2×957.63/1.25 = 1532.21
        assert top[5] == "1.532,21 €" and top[7] == "+332,21 €"
        cards = panel._cards
        assert cards["value"]._lbl_value.text() == "2.958,21 €"
        assert cards["cost"]._lbl_value.text() == "2.200,00 €"
        assert cards["gain"]._lbl_value.text() == "+758,21 €"
        assert cards["xirr"]._lbl_value.text() != "—"
        assert not panel._banner.isVisibleTo(panel)

        index = panel._proxy.index(0, 0)
        panel._on_double_click(index)
        assert view._tabs.currentIndex() == 1 and view._inp_search.text() == "GEV"
    finally:
        view.shutdown()


def test_positions_banner_for_missing_price_and_closed_toggle(env):
    tke = Security(symbol="TKE.DE", name="Take-Two", currency="EUR")
    env.session.add(tke)
    env.session.commit()
    buy(env, tke, 8, 1455.2)
    buy(env, env.enr, 5, 500.0, d=env.day - timedelta(days=10))
    save_transaction(env.session, env.user.id, TransactionDraft(
        security_id=env.enr.id, kind=InvestmentKind.sell, trade_date=env.day,
        broker="DEGIRO", quantity=5, amount_eur=700.0))
    view = make_view(env)
    try:
        panel = view._positions
        assert panel._banner.isVisibleTo(panel) and "TKE.DE" in panel._banner.text()
        assert panel._model.rowCount() == 1
        panel._chk_closed.setChecked(True)
        assert panel._model.rowCount() == 2
        assert panel._cards["realized"]._lbl_value.text() == "+200,00 €"
    finally:
        view.shutdown()


def test_new_user_starts_on_operations_tab(env):
    view = make_view(env)
    try:
        assert view._tabs.currentIndex() == 1
    finally:
        view.shutdown()


# ---------------------------------------------------------------------------
# Evolución / Distribución tabs
# ---------------------------------------------------------------------------

def test_chart_tabs_load_lazily_and_paint(env):
    for offset, close in ((3, 130.0), (2, 135.0)):
        env.session.add(PriceHistory(security_id=env.enr.id,
                                     price_date=env.day - timedelta(days=offset), close=close))
    env.session.commit()
    buy(env, env.enr, 10, 1300.0, d=env.day - timedelta(days=3))
    buy(env, env.gev, 1, 700.0, d=env.day - timedelta(days=3))
    view = make_view(env)
    try:
        assert view._evolution._history.points == []          # not built until visible
        view._tabs.setCurrentWidget(view._evolution)
        points = view._evolution._history.points
        assert len(points) >= 3 and points[-1].value_eur > 0
        assert "plusvalía latente" in view._evolution._lbl_summary.text()

        view._evolution.resize(900, 420)
        view._evolution._canvas.resize(860, 320)
        view._evolution._canvas.hover_x = 500.0
        assert not view._evolution._canvas.grab().isNull()     # paints lines + hover box
        for button in view._evolution._period_group.buttons():
            button.click()

        view._tabs.setCurrentWidget(view._allocation)
        labels = [label for label, _ in view._allocation.data]
        assert labels == ["Siemens Energy AG", "GE Vernova Inc."]
        view._allocation._set_dimension("currency")
        assert [label for label, _ in view._allocation.data] == ["EUR", "USD"]
    finally:
        view.shutdown()


def test_choosing_a_benchmark_catalogues_it_and_refreshes(env):
    buy(env, env.enr, 10, 1300.0)
    view = make_view(env)
    try:
        view._tabs.setCurrentWidget(view._evolution)
        combo = view._evolution._cmb_benchmark
        combo.setCurrentIndex(combo.findData("sp500"))
        assert env.session.query(Security).filter_by(symbol="SXR8.DE").one()
        assert wait_until(lambda: not view._refresher.running)
        combo.setCurrentIndex(0)
        assert view._evolution.benchmark_key is None
    finally:
        view.shutdown()


# ---------------------------------------------------------------------------
# F6: pending dividends/splits, prefilled entry, Fiscalidad tab
# ---------------------------------------------------------------------------

def test_pending_events_banner_register_and_dismiss(env, monkeypatch):
    from app.gui.investment_dialogs import PendingEventsDialog
    from app.investments.market_data import CorporateActions

    buy(env, env.gev, 4, 1000.0, d=env.day - timedelta(days=200))
    env.session.add(FxRate(currency="USD", rate_date=env.day - timedelta(days=300), rate=1.25))
    env.session.commit()
    ex1, ex2 = env.day - timedelta(days=100), env.day - timedelta(days=10)
    env.provider.corporate_actions = lambda symbol: CorporateActions(
        dividends=((ex1, 0.5), (ex2, 0.5)) if symbol == "GEV" else ())
    view = make_view(env)
    try:
        view.check_corporate_actions(force=True)
        assert wait_until(lambda: view._events_banner.isVisibleTo(view))
        assert "2 dividendos" in view._lbl_events.text()

        dialog = PendingEventsDialog(env.session, env.user.id, env.provider, env.theme,
                                     view._pending, view._dismiss_event)
        assert dialog._table.rowCount() == 2
        dialog._table.selectRow(0)
        dialog._dismiss()
        assert dialog._table.rowCount() == 1
        view._update_pending()
        assert "1 dividendo " in view._lbl_events.text() + " "

        # Registering the remaining one through the prefilled form hides the banner.
        event = view._pending[0]
        form = TransactionDialog(env.session, env.user.id, env.provider, env.theme,
                                 default_security_id=event.security_id,
                                 default_kind=InvestmentKind.dividend,
                                 prefill={"trade_date": event.ex_date, "amount_eur": event.gross_eur,
                                          "hint": "Dividendo detectado"})
        assert form._lbl_prefill.isVisibleTo(form)
        assert form._amount.value() == pytest.approx(event.gross_eur)
        form._withheld.setValue(0.3)
        form._save()
        view._update_pending()
        assert not view._events_banner.isVisibleTo(view)
    finally:
        view.shutdown()


def test_tax_tab_shows_report_and_exports(env, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    buy(env, env.enr, 10, 1500.0, d=date(2025, 1, 10))
    save_transaction(env.session, env.user.id, TransactionDraft(
        security_id=env.enr.id, kind=InvestmentKind.sell, trade_date=date(2025, 5, 2),
        broker="DEGIRO", quantity=10, amount_eur=1000.0))
    buy(env, env.enr, 4, 420.0, d=date(2025, 6, 20))
    view = make_view(env)
    target = tmp_path / "informe.xlsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    try:
        view._tabs.setCurrentWidget(view._tax)
        tax = view._tax
        assert tax._cmb_year.currentData() == 2025
        assert tax._sales_model.rowCount() == 1
        assert tax._cards["net"]._lbl_value.text() == "−500,00 €"
        assert tax._cards["blocked"]._lbl_value.text() == "−200,00 €"
        assert tax._cards["computable"]._lbl_value.text() == "−300,00 €"
        tax._export()
        assert target.exists() and shown
    finally:
        view.shutdown()
