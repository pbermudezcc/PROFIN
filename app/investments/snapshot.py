"""
app/investments/snapshot.py
===========================
FAMILYFIN — A user's portfolio priced from the local cache, ready to show.

portfolio_snapshot() joins the FIFO positions (portfolio.py) with the cached
closes and FX rates (prices.py) and returns one row per security plus the
totals behind the KPI cards. It never calls the network.

Missing data rules
  * no cached price  → the position is valued at cost (gain 0) and flagged
    `has_price=False`, so totals stay meaningful and the table says why;
  * no FX rate       → same treatment (a USD price cannot become EUR);
  * price older than STALE_PRICE_DAYS → flagged `stale` (funds publish
    their NAV 2–3 business days late, so a few days is normal).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.models import InvestmentKind, InvestmentTransaction, Security
from app.investments.cash import CashLine, user_cash
from app.investments.portfolio import build_positions, value_position
from app.investments.prices import fx_rate, latest_prices
from app.investments.returns import investor_cash_flows, xirr

STALE_PRICE_DAYS = 7


@dataclass
class PositionRow:
    security:            Security
    quantity:            float
    cost_basis_eur:      float
    average_cost_eur:    Optional[float]
    price:               Optional[float]          # quote currency
    price_date:          Optional[date]
    market_value_eur:    float
    unrealized_gain_eur: float
    unrealized_pct:      Optional[float]
    day_change_eur:      Optional[float]
    day_change_pct:      Optional[float]
    realized_gain_eur:   float
    dividends_eur:       float
    brokers:             List[str]
    has_price:           bool = True
    stale:               bool = False
    weight:              float = 0.0              # share of the open market value

    @property
    def is_open(self) -> bool:
        return self.quantity > 1e-6

    @property
    def total_return_eur(self) -> float:
        """Unrealized + realized + dividends: what the position has earned."""
        return self.unrealized_gain_eur + self.realized_gain_eur + self.dividends_eur


@dataclass
class PortfolioTotals:
    market_value_eur:      float = 0.0
    cost_basis_eur:        float = 0.0
    unrealized_gain_eur:   float = 0.0
    unrealized_pct:        Optional[float] = None
    day_change_eur:        float = 0.0
    day_change_pct:        Optional[float] = None
    realized_ytd_eur:      float = 0.0
    dividends_ytd_eur:     float = 0.0
    realized_total_eur:    float = 0.0
    dividends_total_eur:   float = 0.0
    xirr:                  Optional[float] = None
    open_positions:        int = 0
    missing_prices:        List[str] = field(default_factory=list)
    last_price_date:       Optional[date] = None
    # Uninvested cash at the brokers (cash.py). Not part of market_value_eur,
    # gains, weights or XIRR: those describe the securities.
    cash:                  List[CashLine] = field(default_factory=list)
    cash_eur:              float = 0.0
    cash_missing_fx:       List[str] = field(default_factory=list)

    @property
    def total_value_eur(self) -> float:
        """Securities + cash: the whole account («Cuenta completa» in DEGIRO)."""
        return self.market_value_eur + self.cash_eur


@dataclass
class PortfolioSnapshot:
    rows:   List[PositionRow]
    totals: PortfolioTotals
    as_of:  date

    @property
    def open_rows(self) -> List[PositionRow]:
        return [r for r in self.rows if r.is_open]

    @property
    def closed_rows(self) -> List[PositionRow]:
        return [r for r in self.rows if not r.is_open]


def portfolio_snapshot(
    session: Session,
    user_id: int,
    as_of: Optional[date] = None,
    broker: Optional[str] = None,
) -> PortfolioSnapshot:
    """Price every position of the user (optionally of one broker) at as_of.

    Filtering by broker replays only that broker's operations, which is
    right as long as shares are not moved between brokers (transfers are
    not modelled yet).
    """
    as_of = as_of or date.today()
    query = session.query(InvestmentTransaction).filter(
        InvestmentTransaction.user_id == user_id,
        InvestmentTransaction.trade_date <= as_of,
    )
    if broker:
        query = query.filter(InvestmentTransaction.broker == broker)
    transactions = query.all()

    positions = build_positions(transactions)
    securities: Dict[int, Security] = {
        s.id: s for s in session.query(Security).filter(Security.id.in_(list(positions)))
    }
    brokers: Dict[int, List[str]] = {}
    for txn in sorted(transactions, key=lambda t: t.trade_date):
        names = brokers.setdefault(txn.security_id, [])
        if txn.broker not in names:
            names.append(txn.broker)
    prices = latest_prices(session, positions, as_of=as_of)

    totals = PortfolioTotals()
    rows: List[PositionRow] = []
    previous_value = 0.0
    for security_id, position in positions.items():
        security = securities[security_id]
        quote = prices.get(security_id)
        rate = fx_rate(session, security.currency, on=quote.price_date) if quote else None
        qty = position.quantity
        realized = position.realized_gain_eur
        row_kwargs = dict(
            security=security, quantity=qty, cost_basis_eur=position.cost_basis_eur,
            average_cost_eur=position.average_cost_eur,
            realized_gain_eur=realized, dividends_eur=position.dividends_eur,
            brokers=brokers.get(security_id, []),
        )
        totals.realized_total_eur += realized
        totals.dividends_total_eur += position.dividends_eur
        totals.realized_ytd_eur += sum(
            r.gain_eur for r in position.realized if r.sell_date.year == as_of.year)

        if not position.is_open:
            rows.append(PositionRow(
                price=quote.close if quote else None,
                price_date=quote.price_date if quote else None,
                market_value_eur=0.0, unrealized_gain_eur=0.0, unrealized_pct=None,
                day_change_eur=None, day_change_pct=None, **row_kwargs))
            continue

        totals.open_positions += 1
        if quote is None or rate is None:
            totals.missing_prices.append(security.symbol)
            rows.append(PositionRow(
                price=quote.close if quote else None,
                price_date=quote.price_date if quote else None,
                market_value_eur=position.cost_basis_eur, unrealized_gain_eur=0.0,
                unrealized_pct=None, day_change_eur=None, day_change_pct=None,
                has_price=False, **row_kwargs))
            totals.market_value_eur += position.cost_basis_eur
            totals.cost_basis_eur += position.cost_basis_eur
            continue

        previous_rate = None
        if quote.previous_date is not None:
            previous_rate = fx_rate(session, security.currency, on=quote.previous_date)
        value = value_position(position, quote.close, rate,
                               quote.previous_close, previous_rate)
        day_pct = None
        if value.day_change_eur is not None:
            base = value.market_value_eur - value.day_change_eur
            day_pct = value.day_change_eur / base if base else None
            totals.day_change_eur += value.day_change_eur
            previous_value += base
        else:
            previous_value += value.market_value_eur
        rows.append(PositionRow(
            price=quote.close, price_date=quote.price_date,
            market_value_eur=value.market_value_eur,
            unrealized_gain_eur=value.unrealized_gain_eur,
            unrealized_pct=value.unrealized_pct,
            day_change_eur=value.day_change_eur, day_change_pct=day_pct,
            stale=(as_of - quote.price_date).days > STALE_PRICE_DAYS,
            **row_kwargs))
        totals.market_value_eur += value.market_value_eur
        totals.cost_basis_eur += position.cost_basis_eur
        totals.unrealized_gain_eur += value.unrealized_gain_eur
        if totals.last_price_date is None or quote.price_date > totals.last_price_date:
            totals.last_price_date = quote.price_date

    totals.dividends_ytd_eur = sum(
        float(t.amount_eur) for t in transactions
        if InvestmentKind(t.kind) == InvestmentKind.dividend and t.trade_date.year == as_of.year
    )
    if totals.cost_basis_eur > 0:
        totals.unrealized_pct = totals.unrealized_gain_eur / totals.cost_basis_eur
    if previous_value > 0:
        totals.day_change_pct = totals.day_change_eur / previous_value
    for row in rows:
        if row.is_open and totals.market_value_eur > 0:
            row.weight = row.market_value_eur / totals.market_value_eur
    totals.xirr = xirr(investor_cash_flows(transactions, totals.market_value_eur, as_of))
    totals.cash = user_cash(session, user_id, broker=broker, on=as_of)
    for line in totals.cash:
        if line.amount_eur is None:
            totals.cash_missing_fx.append(line.currency)
        else:
            totals.cash_eur += line.amount_eur

    rows.sort(key=lambda r: (not r.is_open, -r.market_value_eur, r.security.name.lower()))
    return PortfolioSnapshot(rows=rows, totals=totals, as_of=as_of)
