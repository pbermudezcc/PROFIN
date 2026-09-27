"""
tests/test_investments_performance.py
=====================================
Portfolio value history, the "same money in the index" benchmark,
allocation groupings, and benchmark refresh.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db.database import _build_engine
from app.db.models import AssetType, Base, FxRate, InvestmentKind, PriceHistory, Security, User
from app.investments.benchmarks import BENCHMARKS
from app.investments.ledger import TransactionDraft, save_transaction
from app.investments.performance import allocation, ensure_benchmark, portfolio_history
from app.investments.prices import refresh_market_data

D1, D2, D3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)


@pytest.fixture
def db(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'perf.db'}")
    Base.metadata.create_all(engine)
    s = Session(engine)
    user = User(username="pedro", password_hash="x")
    enr = Security(symbol="ENR.DE", name="Siemens Energy AG", currency="EUR")
    gev = Security(symbol="GEV", name="GE Vernova Inc.", currency="USD")
    fund = Security(symbol="0P00008XG7.F", name="Boreas", currency="EUR", asset_type=AssetType.fund)
    s.add_all([user, enr, gev, fund])
    s.flush()
    for day, e, g in ((D1, 100.0, 500.0), (D2, 110.0, 550.0), (D3, 120.0, 600.0)):
        s.add(PriceHistory(security_id=enr.id, price_date=day, close=e))
        s.add(PriceHistory(security_id=gev.id, price_date=day, close=g))
        s.add(FxRate(currency="USD", rate_date=day, rate=1.25))
    s.commit()
    yield SimpleNamespace(s=s, user=user, enr=enr, gev=gev, fund=fund)
    s.close()
    engine.dispose()


def op(db, sec, kind, d, qty=None, eur=0.0, broker="DEGIRO"):
    save_transaction(db.s, db.user.id, TransactionDraft(
        security_id=sec.id, kind=kind, trade_date=d, broker=broker, quantity=qty, amount_eur=eur))


B, S = InvestmentKind.buy, InvestmentKind.sell


def test_history_values_holdings_each_day(db):
    op(db, db.enr, B, D1, 10, 1000.0)
    op(db, db.gev, B, D2, 1, 440.0, broker="EquatePlus")
    points = portfolio_history(db.s, db.user.id, end=D3).points
    assert [p.day for p in points] == [D1, D2, D3]
    assert [round(p.value_eur, 2) for p in points] == [1000.0, 1100.0 + 440.0, 1200.0 + 480.0]
    assert [p.invested_eur for p in points] == [1000.0, 1440.0, 1440.0]
    assert all(p.benchmark_eur is None for p in points)


def test_history_after_a_sale_drops_the_shares(db):
    op(db, db.enr, B, D1, 10, 1000.0)
    op(db, db.enr, S, D2, 5, 550.0)
    last = portfolio_history(db.s, db.user.id, end=D3).points[-1]
    assert last.value_eur == pytest.approx(600.0) and last.invested_eur == pytest.approx(500.0)


def test_security_without_prices_counts_at_cost(db):
    op(db, db.fund, B, D1, 10, 1200.0, broker="Tressis")
    points = portfolio_history(db.s, db.user.id, end=D3).points
    assert [p.day for p in points] == [D1, D3]          # no market days known: start and end
    assert all(p.value_eur == 1200.0 for p in points)


def test_benchmark_follows_same_cash_flows(db):
    bench = ensure_benchmark(db.s, "msci_world")
    for day, close in ((D1, 50.0), (D2, 55.0), (D3, 60.0)):
        db.s.add(PriceHistory(security_id=bench.id, price_date=day, close=close))
    db.s.commit()
    op(db, db.enr, B, D1, 10, 1000.0)      # 20 index units
    op(db, db.enr, S, D2, 5, 550.0)        # sells 10 units
    history = portfolio_history(db.s, db.user.id, end=D3, benchmark_key="msci_world")
    assert history.benchmark_name == BENCHMARKS["msci_world"].name
    assert [round(p.benchmark_eur, 2) for p in history.points] == [1000.0, 550.0, 600.0]


def test_history_since_and_empty(db):
    op(db, db.enr, B, D1, 10, 1000.0)
    history = portfolio_history(db.s, db.user.id, end=D3)
    assert [p.day for p in history.since(D2)] == [D2, D3]
    assert portfolio_history(db.s, 999, end=D3).points == []


def test_allocation_by_each_dimension(db):
    op(db, db.enr, B, D1, 10, 1000.0, broker="Trade Republic")
    op(db, db.gev, B, D1, 1, 400.0, broker="EquatePlus")
    op(db, db.fund, B, D1, 1, 300.0, broker="Tressis")
    op(db, db.enr, B, D2, 5, 550.0, broker="DEGIRO")
    assert allocation(db.s, db.user.id, "security", as_of=D3) == [
        ("Siemens Energy AG", 1800.0), ("GE Vernova Inc.", 480.0), ("Boreas", 300.0)]
    assert allocation(db.s, db.user.id, "broker", as_of=D3) == [
        ("Trade Republic", 1200.0), ("DEGIRO", 600.0), ("EquatePlus", 480.0), ("Tressis", 300.0)]
    assert allocation(db.s, db.user.id, "currency", as_of=D3) == [("EUR", 2100.0), ("USD", 480.0)]
    assert allocation(db.s, db.user.id, "asset_type", as_of=D3) == [("Acciones", 2280.0), ("Fondos", 300.0)]


class _Provider:
    name = "fake"

    def __init__(self):
        self.calls = []

    def daily_closes(self, symbols, start, end=None):
        self.calls.append((sorted(symbols), start))
        return {}


def test_benchmarks_are_refreshed_only_when_there_are_operations(db):
    ensure_benchmark(db.s, "sp500")
    provider = _Provider()
    refresh_market_data(db.s, provider, today=D3)
    assert provider.calls == []
    op(db, db.enr, B, D1, 10, 1000.0)
    refresh_market_data(db.s, provider, today=D3)
    symbols, start = provider.calls[0]
    assert "SXR8.DE" in symbols and start == D1 - timedelta(days=7)
