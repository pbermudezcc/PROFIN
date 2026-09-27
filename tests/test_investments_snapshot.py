"""
tests/test_investments_snapshot.py
==================================
Portfolio snapshot behind the Posiciones tab: valuation with FX, day
change, weights, realized/dividends of the year, closed positions,
missing/stale prices, broker filter and per-user isolation.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db.database import _build_engine
from app.db.models import Base, FxRate, InvestmentKind, PriceHistory, Security, User
from app.investments.ledger import TransactionDraft, save_transaction
from app.investments.snapshot import portfolio_snapshot

AS_OF = date(2026, 9, 25)
PREV = date(2026, 9, 24)


@pytest.fixture
def db(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'snap.db'}")
    Base.metadata.create_all(engine)
    s = Session(engine)
    pedro = User(username="pedro", password_hash="x")
    maria = User(username="maria", password_hash="x")
    gev = Security(symbol="GEV", name="GE Vernova Inc.", currency="USD")
    enr = Security(symbol="ENR.DE", name="Siemens Energy AG", currency="EUR")
    tke = Security(symbol="TKE.DE", name="Take-Two", currency="EUR")
    fund = Security(symbol="0P00008XG7.F", name="Boreas", currency="EUR")
    s.add_all([pedro, maria, gev, enr, tke, fund])
    s.flush()
    s.add_all([
        PriceHistory(security_id=gev.id, price_date=PREV, close=950.0),
        PriceHistory(security_id=gev.id, price_date=AS_OF, close=1000.0),
        PriceHistory(security_id=enr.id, price_date=PREV, close=140.0),
        PriceHistory(security_id=enr.id, price_date=AS_OF, close=150.0),
        PriceHistory(security_id=fund.id, price_date=date(2026, 9, 1), close=146.0),
        FxRate(currency="USD", rate_date=PREV, rate=1.25),
        FxRate(currency="USD", rate_date=AS_OF, rate=1.25),
    ])
    s.commit()
    yield SimpleNamespace(s=s, pedro=pedro, maria=maria, gev=gev, enr=enr, tke=tke, fund=fund)
    s.close()
    engine.dispose()


def op(db, user, sec, kind, d, qty=None, eur=0.0, broker="DEGIRO", **kw):
    save_transaction(db.s, user.id, TransactionDraft(
        security_id=sec.id, kind=kind, trade_date=d, broker=broker,
        quantity=qty, amount_eur=eur, **kw))


B, S, D = InvestmentKind.buy, InvestmentKind.sell, InvestmentKind.dividend


def test_values_positions_in_eur_with_day_change_and_weights(db):
    op(db, db.pedro, db.gev, B, date(2025, 3, 3), 2, 1200.0, broker="EquatePlus")   # 2×1000/1.25 = 1600
    op(db, db.pedro, db.enr, B, date(2025, 2, 3), 10, 1000.0)                        # 10×150 = 1500
    snap = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF)
    gev, enr = snap.rows                          # sorted by market value
    assert gev.security.symbol == "GEV"
    assert gev.market_value_eur == pytest.approx(1600.0)
    assert gev.unrealized_gain_eur == pytest.approx(400.0)
    assert gev.unrealized_pct == pytest.approx(400 / 1200)
    assert gev.day_change_eur == pytest.approx(2 * 50 / 1.25)
    assert gev.day_change_pct == pytest.approx(50 / 950)
    assert enr.day_change_eur == pytest.approx(100.0)
    assert gev.weight + enr.weight == pytest.approx(1.0)
    assert gev.brokers == ["EquatePlus"]

    totals = snap.totals
    assert totals.market_value_eur == pytest.approx(3100.0)
    assert totals.cost_basis_eur == pytest.approx(2200.0)
    assert totals.unrealized_gain_eur == pytest.approx(900.0)
    assert totals.day_change_eur == pytest.approx(180.0)
    assert totals.day_change_pct == pytest.approx(180.0 / (3100.0 - 180.0))
    assert totals.open_positions == 2 and totals.missing_prices == []
    assert totals.xirr is not None and totals.xirr > 0
    assert totals.last_price_date == AS_OF


def test_realized_and_dividends_of_the_year_and_closed_positions(db):
    op(db, db.pedro, db.enr, B, date(2025, 1, 2), 10, 1000.0)
    op(db, db.pedro, db.enr, S, date(2025, 6, 2), 4, 480.0)      # +80 in 2025
    op(db, db.pedro, db.enr, S, date(2026, 6, 2), 6, 840.0)      # +240 in 2026 → closed
    op(db, db.pedro, db.enr, D, date(2025, 5, 2), eur=5.0)
    op(db, db.pedro, db.enr, D, date(2026, 5, 2), eur=7.0, tax_withheld_eur=1.0)
    snap = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF)
    assert snap.open_rows == []
    closed = snap.closed_rows[0]
    assert closed.realized_gain_eur == pytest.approx(320.0)
    assert closed.total_return_eur == pytest.approx(332.0)
    t = snap.totals
    assert (t.realized_ytd_eur, t.realized_total_eur) == (pytest.approx(240.0), pytest.approx(320.0))
    assert (t.dividends_ytd_eur, t.dividends_total_eur) == (pytest.approx(7.0), pytest.approx(12.0))
    assert t.market_value_eur == 0 and t.open_positions == 0


def test_missing_price_is_valued_at_cost_and_listed(db):
    op(db, db.pedro, db.tke, B, date(2025, 4, 10), 8, 1455.2)
    snap = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF)
    row = snap.rows[0]
    assert not row.has_price and row.market_value_eur == pytest.approx(1455.2)
    assert row.unrealized_gain_eur == 0
    assert snap.totals.missing_prices == ["TKE.DE"]


def test_old_fund_nav_is_flagged_stale(db):
    op(db, db.pedro, db.fund, B, date(2025, 1, 15), 10, 1200.0, broker="Tressis")
    row = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF).rows[0]
    assert row.has_price and row.stale and row.day_change_eur is None
    assert row.market_value_eur == pytest.approx(1460.0)


def test_broker_filter_and_user_isolation(db):
    op(db, db.pedro, db.gev, B, date(2025, 3, 3), 2, 1200.0, broker="EquatePlus")
    op(db, db.pedro, db.enr, B, date(2025, 2, 3), 10, 1000.0, broker="Trade Republic")
    op(db, db.maria, db.enr, B, date(2025, 2, 3), 99, 9900.0, broker="Trade Republic")
    only_tr = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF, broker="Trade Republic")
    assert [r.security.symbol for r in only_tr.rows] == ["ENR.DE"]
    assert only_tr.rows[0].quantity == 10
    assert portfolio_snapshot(db.s, db.maria.id, as_of=AS_OF).rows[0].quantity == 99


def test_operations_after_as_of_are_ignored(db):
    op(db, db.pedro, db.enr, B, date(2025, 2, 3), 10, 1000.0)
    op(db, db.pedro, db.enr, B, date(2026, 9, 25), 10, 1500.0)
    assert portfolio_snapshot(db.s, db.pedro.id, as_of=PREV).rows[0].quantity == 10


def test_empty_portfolio(db):
    snap = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF)
    assert snap.rows == [] and snap.totals.market_value_eur == 0 and snap.totals.xirr is None
