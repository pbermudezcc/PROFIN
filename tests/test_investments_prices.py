"""
tests/test_investments_prices.py
================================
Market-data layer of the Inversiones module: the price/FX cache, the refresh
window, manual prices, the securities catalogue, Yahoo response parsing and
the background refresher. A fake provider stands in for Yahoo; no test
touches the network.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import _build_engine
from app.db.models import (
    PRICE_SOURCE_MANUAL,
    AssetType,
    Base,
    FxRate,
    InvestmentKind,
    InvestmentTransaction,
    PriceHistory,
    Security,
    User,
)
from app.investments.market_data import (
    MarketDataError,
    SecurityInfo,
    YahooProvider,
    fx_pair,
)
from app.investments.prices import (
    OVERLAP_DAYS,
    find_or_create_security,
    fx_rate,
    is_stale,
    latest_prices,
    refresh_market_data,
    set_manual_price,
)
from app.investments.refresh_worker import PriceRefresher

TODAY = date(2026, 9, 25)


class FakeProvider:
    name = "Fake"

    def __init__(self, closes=None, fail=False):
        self.closes = closes or {}
        self.fail = fail
        self.calls = []
        self.info_calls = []

    def search(self, text, limit=10):
        return []

    def security_info(self, symbol):
        self.info_calls.append(symbol)
        if symbol == "NOPE":
            raise MarketDataError("Yahoo Finance no tiene datos del valor NOPE.")
        return SecurityInfo(symbol=symbol, name=f"{symbol} Inc.", asset_type=AssetType.stock,
                            currency="USD", exchange="NYSE", sector="Industrials",
                            country="United States")

    def daily_closes(self, symbols, start, end=None):
        self.calls.append((sorted(symbols), start))
        if self.fail:
            raise MarketDataError("No se pudo conectar con Yahoo Finance.")
        return {s: [(d, v) for d, v in rows if d >= start]
                for s, rows in self.closes.items() if s in symbols}


def days(start, values):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


@pytest.fixture
def db(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'prices.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    user = User(username="pedro", password_hash="x")
    gev = Security(symbol="GEV", name="GE Vernova", currency="USD", exchange="NYSE")
    enr = Security(symbol="ENR.DE", name="Siemens Energy", currency="EUR", exchange="XETRA")
    idle = Security(symbol="IDLE", name="Nobody holds it", currency="EUR")
    fund = Security(symbol="0P00008XG7.F", name="Boreas", currency="EUR",
                    asset_type=AssetType.fund, price_source=PRICE_SOURCE_MANUAL)
    session.add_all([user, gev, enr, idle, fund])
    session.flush()
    for sec, d in ((gev, date(2026, 9, 10)), (enr, date(2026, 9, 15)), (fund, date(2026, 9, 1))):
        session.add(InvestmentTransaction(
            user_id=user.id, security_id=sec.id, broker="DEGIRO", kind=InvestmentKind.buy,
            trade_date=d, quantity=1, price=100.0, currency=sec.currency, amount_eur=100.0,
        ))
    session.commit()
    yield SimpleNamespace(session=session, factory=factory, gev=gev, enr=enr,
                          idle=idle, fund=fund, user=user)
    session.close()
    engine.dispose()


def full_closes():
    return {
        "GEV": days(date(2026, 9, 1), [900 + i for i in range(25)]),
        "ENR.DE": days(date(2026, 9, 1), [140 + i for i in range(25)]),
        "EURUSD=X": days(date(2026, 9, 1), [1.14] * 25),
        "IDLE": days(date(2026, 9, 1), [1.0] * 25),
    }


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def test_first_refresh_downloads_from_first_trade_and_fx(db):
    provider = FakeProvider(full_closes())
    result = refresh_market_data(db.session, provider, today=TODAY)

    assert result.ok
    # Only held, provider-priced securities; the fund is manual, IDLE has no trades.
    assert provider.calls[0][0] == ["ENR.DE", "EURUSD=X", "GEV"]
    assert provider.calls[0][1] == date(2026, 9, 3)          # GEV first trade − 7 days
    assert sorted(result.updated) == ["ENR.DE", "GEV"]
    gev_dates = [r.price_date for r in db.session.query(PriceHistory)
                 .filter_by(security_id=db.gev.id).order_by(PriceHistory.price_date)]
    enr_first = (db.session.query(PriceHistory).filter_by(security_id=db.enr.id)
                 .order_by(PriceHistory.price_date).first().price_date)
    assert gev_dates[0] == date(2026, 9, 3)
    assert enr_first == date(2026, 9, 8)                     # its own window
    assert db.session.query(FxRate).filter_by(currency="USD").count() > 0


def test_next_refresh_only_asks_for_the_overlap_window(db):
    provider = FakeProvider(full_closes())
    refresh_market_data(db.session, provider, today=TODAY)
    refresh_market_data(db.session, provider, today=TODAY)
    last_cached = date(2026, 9, 25)
    assert provider.calls[1][1] == last_cached - timedelta(days=OVERLAP_DAYS)


def test_refresh_replaces_intraday_value_with_final_close(db):
    closes = full_closes()
    refresh_market_data(db.session, FakeProvider(closes), today=TODAY)
    closes["GEV"][-1] = (closes["GEV"][-1][0], 999.99)
    refresh_market_data(db.session, FakeProvider(closes), today=TODAY)
    latest = latest_prices(db.session, [db.gev.id])[db.gev.id]
    assert latest.close == pytest.approx(999.99)


def test_refresh_never_overwrites_manual_prices(db):
    set_manual_price(db.session, db.gev.id, date(2026, 9, 24), 1234.0)
    refresh_market_data(db.session, FakeProvider(full_closes()), today=TODAY)
    row = db.session.get(PriceHistory, (db.gev.id, date(2026, 9, 24)))
    assert row.close == 1234.0 and row.source == PRICE_SOURCE_MANUAL


def test_provider_failure_keeps_cache_and_reports_spanish_error(db):
    refresh_market_data(db.session, FakeProvider(full_closes()), today=TODAY)
    before = db.session.query(PriceHistory).count()
    result = refresh_market_data(db.session, FakeProvider(fail=True), today=TODAY)
    assert not result.ok and "Yahoo" in result.error
    assert db.session.query(PriceHistory).count() == before


def test_symbols_without_data_are_listed_as_missing(db):
    closes = full_closes()
    del closes["ENR.DE"]
    result = refresh_market_data(db.session, FakeProvider(closes), today=TODAY)
    assert result.ok and result.missing == ["ENR.DE"]


def test_nothing_to_refresh_does_not_call_the_provider(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)
    provider = FakeProvider()
    with Session(engine) as session:
        assert refresh_market_data(session, provider, today=TODAY).ok
    assert provider.calls == []
    engine.dispose()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def test_latest_prices_include_previous_close_and_respect_as_of(db):
    refresh_market_data(db.session, FakeProvider(full_closes()), today=TODAY)
    latest = latest_prices(db.session, [db.gev.id, db.idle.id])
    assert set(latest) == {db.gev.id}
    gev = latest[db.gev.id]
    assert (gev.price_date, gev.close) == (date(2026, 9, 25), 924.0)
    assert (gev.previous_date, gev.previous_close) == (date(2026, 9, 24), 923.0)
    older = latest_prices(db.session, [db.gev.id], as_of=date(2026, 9, 10))[db.gev.id]
    assert older.close == 909.0


def test_fx_rate_eur_minor_units_and_dates(db):
    s = db.session
    s.add_all([FxRate(currency="USD", rate_date=date(2026, 9, 1), rate=1.10),
               FxRate(currency="USD", rate_date=date(2026, 9, 20), rate=1.14),
               FxRate(currency="GBP", rate_date=date(2026, 9, 20), rate=0.85)])
    s.commit()
    assert fx_rate(s, "EUR") == 1.0
    assert fx_rate(s, "USD") == 1.14
    assert fx_rate(s, "USD", on=date(2026, 9, 10)) == 1.10
    assert fx_rate(s, "USD", on=date(2026, 8, 1)) is None
    assert fx_rate(s, "GBp") == pytest.approx(85.0)
    assert fx_pair("GBp") == ("GBP", 100.0) and fx_pair("EUR") is None


def test_manual_price_must_be_positive(db):
    with pytest.raises(ValueError):
        set_manual_price(db.session, db.fund.id, TODAY, 0)


def test_is_stale_after_fifteen_minutes():
    now = datetime(2026, 9, 27, 12, 0)
    assert is_stale(None, now)
    assert not is_stale(now - timedelta(minutes=14), now)
    assert is_stale(now - timedelta(minutes=15), now)


# ---------------------------------------------------------------------------
# Securities catalogue
# ---------------------------------------------------------------------------

def test_find_or_create_security_asks_provider_only_once(db):
    provider = FakeProvider()
    first = find_or_create_security(db.session, provider, " amzn ", isin="us0231351067")
    again = find_or_create_security(db.session, provider, "AMZN")
    assert first is again
    assert provider.info_calls == ["AMZN"]
    assert (first.currency, first.exchange, first.isin) == ("USD", "NYSE", "US0231351067")


def test_find_or_create_security_unknown_symbol_raises(db):
    with pytest.raises(MarketDataError):
        find_or_create_security(db.session, FakeProvider(), "NOPE")


# ---------------------------------------------------------------------------
# Yahoo parsing (yfinance replaced by a stub; no network)
# ---------------------------------------------------------------------------

def _stub_yf(frame=None, error=None, quotes=None, info=None):
    def download(*args, **kwargs):
        if error:
            raise error
        return frame

    return SimpleNamespace(
        download=download,
        Search=lambda *a, **k: SimpleNamespace(quotes=quotes or []),
        Ticker=lambda symbol: SimpleNamespace(info=info or {}),
    )


def test_yahoo_daily_closes_parses_multiindex_and_drops_nan(monkeypatch):
    index = pd.to_datetime(["2026-09-24", "2026-09-25"])
    columns = pd.MultiIndex.from_product([["Close", "Open"], ["AMZ.DE", "GEV"]])
    frame = pd.DataFrame([[216.75, 955.0, 1, 1], [218.45, float("nan"), 1, 1]],
                         index=index, columns=columns)
    provider = YahooProvider()
    monkeypatch.setattr(provider, "_yf", lambda: _stub_yf(frame))
    closes = provider.daily_closes(["GEV", "AMZ.DE", "MISSING"], start=date(2026, 9, 1))
    assert closes["AMZ.DE"] == [(date(2026, 9, 24), 216.75), (date(2026, 9, 25), 218.45)]
    assert closes["GEV"] == [(date(2026, 9, 24), 955.0)]
    assert "MISSING" not in closes


def test_yahoo_network_errors_become_market_data_errors(monkeypatch):
    provider = YahooProvider()
    monkeypatch.setattr(provider, "_yf", lambda: _stub_yf(error=OSError("offline")))
    with pytest.raises(MarketDataError, match="conexión"):
        provider.daily_closes(["GEV"], start=date(2026, 9, 1))
    monkeypatch.setattr(provider, "_yf", lambda: _stub_yf(frame=pd.DataFrame()))
    with pytest.raises(MarketDataError):
        provider.daily_closes(["GEV"], start=date(2026, 9, 1))


def test_yahoo_search_keeps_only_supported_asset_types(monkeypatch):
    quotes = [
        {"symbol": "ENR.DE", "quoteType": "EQUITY", "exchDisp": "XETRA", "longname": "Siemens Energy AG"},
        {"symbol": "0P00008XG7.F", "quoteType": "MUTUALFUND", "exchDisp": "Frankfurt",
         "longname": "Boreas Cartera Crecimiento R FI"},
        {"symbol": "BTC-EUR", "quoteType": "CRYPTOCURRENCY", "shortname": "Bitcoin EUR"},
    ]
    provider = YahooProvider()
    monkeypatch.setattr(provider, "_yf", lambda: _stub_yf(quotes=quotes))
    results = provider.search("siemens")
    assert [(r.symbol, r.asset_type) for r in results] == [
        ("ENR.DE", AssetType.stock), ("0P00008XG7.F", AssetType.fund),
    ]
    assert provider.search("   ") == []


def test_yahoo_security_info_maps_fields(monkeypatch):
    info = {"longName": "GE Vernova Inc.", "quoteType": "EQUITY", "currency": "USD",
            "fullExchangeName": "NYSE", "sector": "Industrials", "country": "United States"}
    provider = YahooProvider()
    monkeypatch.setattr(provider, "_yf", lambda: _stub_yf(info=info))
    got = provider.security_info("GEV")
    assert (got.name, got.currency, got.exchange, got.asset_type) == (
        "GE Vernova Inc.", "USD", "NYSE", AssetType.stock)
    monkeypatch.setattr(provider, "_yf", lambda: _stub_yf(info={}))
    with pytest.raises(MarketDataError):
        provider.security_info("XXXX")


# ---------------------------------------------------------------------------
# Background refresher
# ---------------------------------------------------------------------------

def _wait_for(signal, timeout_ms=10000):
    received = []
    loop = QEventLoop()
    signal.connect(lambda value: (received.append(value), loop.quit()))
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return received


def test_refresher_runs_in_background_and_tracks_last_success(db):
    app = QApplication.instance() or QApplication([])
    refresher = PriceRefresher(provider=FakeProvider(full_closes()),
                               session_factory=db.factory)
    assert refresher.refresh()
    assert not refresher.refresh()              # one refresh at a time
    received = _wait_for(refresher.finished)
    assert received and received[0].ok
    assert refresher.last_success is not None and not refresher.running
    assert not refresher.refresh_if_stale()     # fresh: nothing to do
    db.session.expire_all()
    assert db.session.query(PriceHistory).count() > 0
    assert app is not None


def test_refresher_failure_does_not_mark_success(db):
    QApplication.instance() or QApplication([])
    refresher = PriceRefresher(provider=FakeProvider(fail=True), session_factory=db.factory)
    refresher.refresh()
    received = _wait_for(refresher.finished)
    assert received and not received[0].ok
    assert refresher.last_success is None
