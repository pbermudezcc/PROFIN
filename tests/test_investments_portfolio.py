"""
tests/test_investments_portfolio.py
===================================
Positions, FIFO lots, realized gains, valuation and XIRR of the Inversiones
module. Pure calculations use plain dataclasses; one test goes through the
ORM to prove the engine reads real InvestmentTransaction rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import _build_engine
from app.db.models import (
    AssetType,
    Base,
    InvestmentKind,
    InvestmentTransaction,
    PriceHistory,
    Security,
    User,
)
from app.investments.portfolio import (
    LedgerError,
    build_positions,
    ledger_error,
    realized_by_year,
    validate_fields,
    value_position,
)
from app.investments.returns import investor_cash_flows, xirr


@dataclass
class T:
    kind: InvestmentKind
    trade_date: date
    quantity: Optional[float] = None
    amount_eur: float = 0.0
    security_id: int = 1
    fees_eur: float = 0.0
    tax_withheld_eur: float = 0.0
    cash_paid_eur: Optional[float] = None
    split_ratio: Optional[float] = None
    id: Optional[int] = None


def buy(d, qty, eur, **kw):
    return T(InvestmentKind.buy, d, qty, eur, **kw)


def sell(d, qty, eur, **kw):
    return T(InvestmentKind.sell, d, qty, eur, **kw)


# ---------------------------------------------------------------------------
# FIFO and positions
# ---------------------------------------------------------------------------

def test_sale_consumes_oldest_lots_first():
    ledger = [
        buy(date(2024, 1, 10), 10, 1000.0, id=1),   # 100 €/share
        buy(date(2024, 6, 10), 10, 1500.0, id=2),   # 150 €/share
        sell(date(2025, 3, 1), 15, 2400.0, id=3),   # 160 €/share
    ]
    position = build_positions(ledger)[1]

    assert [(r.acquired, r.quantity) for r in position.realized] == [
        (date(2024, 1, 10), 10), (date(2024, 6, 10), 5),
    ]
    first, second = position.realized
    assert first.cost_eur == pytest.approx(1000.0)
    assert first.proceeds_eur == pytest.approx(1600.0)
    assert second.cost_eur == pytest.approx(750.0)
    assert second.proceeds_eur == pytest.approx(800.0)
    assert position.realized_gain_eur == pytest.approx(650.0)
    # What is left is the newer lot at its own price.
    assert position.quantity == pytest.approx(5)
    assert position.cost_basis_eur == pytest.approx(750.0)
    assert position.average_cost_eur == pytest.approx(150.0)


def test_ledger_order_does_not_matter_only_dates():
    ledger = [
        sell(date(2025, 3, 1), 15, 2400.0, id=3),
        buy(date(2024, 6, 10), 10, 1500.0, id=2),
        buy(date(2024, 1, 10), 10, 1000.0, id=1),
    ]
    assert build_positions(ledger)[1].realized_gain_eur == pytest.approx(650.0)


def test_selling_more_than_held_is_rejected_in_spanish():
    ledger = [buy(date(2024, 1, 10), 10, 1000.0), sell(date(2024, 2, 1), 11, 1200.0)]
    with pytest.raises(LedgerError, match="solo había 10"):
        build_positions(ledger)


def test_sale_dated_before_the_purchase_is_rejected():
    ledger = [buy(date(2024, 5, 1), 10, 1000.0), sell(date(2024, 4, 1), 5, 600.0)]
    with pytest.raises(LedgerError, match="01/04/2024"):
        build_positions(ledger)


def test_same_day_buy_and_sell_replays_buy_first():
    d = date(2024, 5, 1)
    ledger = [sell(d, 10, 1100.0, id=2), buy(d, 10, 1000.0, id=1)]
    position = build_positions(ledger)[1]
    assert not position.is_open
    assert position.realized_gain_eur == pytest.approx(100.0)


def test_fractional_fund_units_sold_in_full_leave_no_residue():
    ledger = [
        buy(date(2023, 1, 2), 6.803561, 1000.0),
        buy(date(2023, 2, 1), 3.333333, 500.0),
        sell(date(2024, 1, 2), 10.136894, 1700.0),
    ]
    position = build_positions(ledger)[1]
    assert not position.is_open
    assert position.lots == []
    assert position.realized_gain_eur == pytest.approx(200.0)


def test_split_multiplies_shares_and_keeps_cost():
    ledger = [
        buy(date(2024, 1, 1), 10, 1000.0),
        T(InvestmentKind.split, date(2024, 6, 1), split_ratio=2.0),
        sell(date(2024, 7, 1), 20, 1200.0),
    ]
    position = build_positions(ledger)[1]
    assert position.realized[0].quantity == pytest.approx(20)
    assert position.realized_gain_eur == pytest.approx(200.0)


def test_grant_keeps_tax_basis_and_cash_paid_apart():
    ledger = [
        T(InvestmentKind.grant, date(2024, 3, 1), 10, 1000.0, cash_paid_eur=0.0),
        sell(date(2024, 9, 1), 10, 1300.0),
    ]
    position = build_positions(ledger)[1]
    # Tax: gain over the market value that was already taxed as salary.
    assert position.realized_gain_eur == pytest.approx(300.0)


def test_positions_are_kept_per_security():
    ledger = [
        buy(date(2024, 1, 1), 10, 1000.0, security_id=1),
        buy(date(2024, 1, 1), 5, 900.0, security_id=2),
        sell(date(2024, 2, 1), 5, 1000.0, security_id=2),
    ]
    positions = build_positions(ledger)
    assert positions[1].quantity == pytest.approx(10)
    assert not positions[2].is_open


def test_dividends_are_accumulated_without_changing_shares():
    ledger = [
        buy(date(2024, 1, 1), 10, 1000.0),
        T(InvestmentKind.dividend, date(2024, 6, 1), amount_eur=8.5, tax_withheld_eur=1.5),
        T(InvestmentKind.dividend, date(2024, 12, 1), amount_eur=8.5, tax_withheld_eur=1.5),
    ]
    position = build_positions(ledger)[1]
    assert position.quantity == pytest.approx(10)
    assert position.dividends_eur == pytest.approx(17.0)
    assert position.dividends_withheld_eur == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("txn, message", [
    (buy(date(2024, 1, 1), 0, 100.0), "acciones"),
    (buy(date(2024, 1, 1), 1, 0.0), "mayor que cero"),
    (sell(date(2024, 1, 1), 1, -5.0), "negativo"),
    (T(InvestmentKind.dividend, date(2024, 1, 1), amount_eur=0.0), "dividendo"),
    (T(InvestmentKind.split, date(2024, 1, 1), split_ratio=1.0), "split"),
    (buy(date(2024, 1, 1), 1, 100.0, fees_eur=-1.0), "Comisiones"),
])
def test_validate_fields_reports_problems_in_spanish(txn, message):
    errors = validate_fields(txn)
    assert errors and message in " ".join(errors)


def test_editing_an_old_buy_down_is_caught_by_the_later_sale():
    ledger = [buy(date(2024, 1, 1), 10, 1000.0, id=1), sell(date(2024, 5, 1), 8, 900.0, id=2)]
    assert ledger_error(ledger) is None
    edited = [buy(date(2024, 1, 1), 5, 500.0, id=1), ledger[1]]
    error = ledger_error(edited)
    assert error is not None and error.transaction is ledger[1]


# ---------------------------------------------------------------------------
# Valuation and yearly report
# ---------------------------------------------------------------------------

def test_value_position_converts_usd_to_eur():
    position = build_positions([buy(date(2024, 1, 1), 10, 900.0)])[1]
    value = value_position(position, price=110.0, fx_rate=1.10,
                           previous_price=99.0, previous_fx_rate=1.10)
    assert value.market_value_eur == pytest.approx(1000.0)
    assert value.unrealized_gain_eur == pytest.approx(100.0)
    assert value.unrealized_pct == pytest.approx(100.0 / 900.0)
    assert value.day_change_eur == pytest.approx(100.0)


def test_value_position_eur_security_without_previous_close():
    position = build_positions([buy(date(2024, 1, 1), 2, 300.0)])[1]
    value = value_position(position, price=142.6)
    assert value.market_value_eur == pytest.approx(285.2)
    assert value.day_change_eur is None


def test_realized_by_year_splits_gains_losses_and_dividends():
    ledger = [
        buy(date(2023, 1, 1), 10, 1000.0, security_id=1),
        buy(date(2023, 1, 1), 10, 1000.0, security_id=2),
        sell(date(2024, 5, 1), 10, 1300.0, security_id=1),   # +300 in 2024
        sell(date(2025, 5, 1), 10, 800.0, security_id=2),    # -200 in 2025
        T(InvestmentKind.dividend, date(2024, 6, 1), amount_eur=17.0,
          tax_withheld_eur=3.0, security_id=2),
    ]
    years = realized_by_year(ledger)
    assert list(years) == [2024, 2025]
    assert years[2024].gains_eur == pytest.approx(300.0)
    assert years[2024].dividends_eur == pytest.approx(17.0)
    assert years[2024].withheld_eur == pytest.approx(3.0)
    assert years[2025].losses_eur == pytest.approx(-200.0)
    assert years[2025].net_gain_eur == pytest.approx(-200.0)


# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------

def test_xirr_of_one_year_ten_percent():
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    assert xirr(flows) == pytest.approx(0.10, abs=1e-6)


def test_xirr_accounts_for_timing_of_contributions():
    # Same +10 % money gain, but half the money was invested for only half the year.
    flows = [
        (date(2025, 1, 1), -1000.0),
        (date(2025, 7, 2), -1000.0),
        (date(2026, 1, 1), 2150.0),
    ]
    rate = xirr(flows)
    assert rate is not None and rate > 0.10


@pytest.mark.parametrize("flows", [
    [],
    [(date(2025, 1, 1), -1000.0)],
    [(date(2025, 1, 1), -1000.0), (date(2025, 6, 1), -10.0)],
])
def test_xirr_undefined_cases_return_none(flows):
    assert xirr(flows) is None


def test_investor_cash_flows_signs_and_grant_cash():
    ledger = [
        buy(date(2024, 1, 1), 10, 1000.0),
        T(InvestmentKind.grant, date(2024, 2, 1), 5, 600.0, cash_paid_eur=0.0),
        T(InvestmentKind.grant, date(2024, 3, 1), 5, 600.0, cash_paid_eur=510.0),
        T(InvestmentKind.dividend, date(2024, 6, 1), amount_eur=20.0),
        sell(date(2024, 9, 1), 5, 700.0),
        T(InvestmentKind.split, date(2024, 10, 1), split_ratio=2.0),
    ]
    flows = investor_cash_flows(ledger, market_value_eur=2500.0, as_of=date(2025, 1, 1))
    assert flows == [
        (date(2024, 1, 1), -1000.0),
        (date(2024, 3, 1), -510.0),
        (date(2024, 6, 1), 20.0),
        (date(2024, 9, 1), 700.0),
        (date(2025, 1, 1), 2500.0),
    ]


# ---------------------------------------------------------------------------
# ORM
# ---------------------------------------------------------------------------

@pytest.fixture
def orm_session(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'inv.db'}")   # foreign keys ON
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_engine_reads_orm_rows_and_security_with_trades_cannot_be_deleted(orm_session):
    s = orm_session
    user = User(username="pedro", password_hash="x")
    gev = Security(symbol="GEV", name="GE Vernova Inc.", asset_type=AssetType.stock,
                   currency="USD", exchange="NYSE")
    s.add_all([user, gev])
    s.flush()
    s.add_all([
        InvestmentTransaction(user_id=user.id, security_id=gev.id, broker="EquatePlus",
                              kind=InvestmentKind.buy, trade_date=date(2025, 3, 3),
                              quantity=2, price=300.0, currency="USD", amount_eur=575.40),
        InvestmentTransaction(user_id=user.id, security_id=gev.id, broker="EquatePlus",
                              kind=InvestmentKind.sell, trade_date=date(2026, 3, 3),
                              quantity=1, price=800.0, currency="USD", amount_eur=700.0),
        PriceHistory(security_id=gev.id, price_date=date(2026, 9, 25), close=957.63),
    ])
    s.commit()

    rows = s.query(InvestmentTransaction).filter_by(user_id=user.id).all()
    position = build_positions(rows)[gev.id]
    assert position.quantity == pytest.approx(1)
    assert position.realized_gain_eur == pytest.approx(700.0 - 287.70)

    s.delete(gev)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # Deleting the user removes their ledger (cascade) but not the shared security.
    s.delete(s.get(User, user.id))
    s.commit()
    assert s.query(InvestmentTransaction).count() == 0
    assert s.query(Security).count() == 1
