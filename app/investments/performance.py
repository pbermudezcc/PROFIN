"""
app/investments/performance.py
==============================
FAMILYFIN — Portfolio value over time, benchmark comparison and allocation.

portfolio_history()
    One point per market day from the first operation to `end`:
      value_eur     market value of the holdings that day (last known close
                    and FX rate; a security without any close yet counts at cost)
      invested_eur  FIFO cost of those holdings — the "Capital invertido" card
      benchmark_eur the same cash flows invested in a benchmark instead: each
                    buy puts its EUR into the index, each sale/dividend takes its
                    EUR out. The gap between the two lines is what stock picking
                    gained or lost against simply buying the index.

allocation()
    Current market value grouped by security, broker, currency or asset type.

Benchmarks are catalogued as ordinary securities (EUR listings, so no FX)
and refreshed together with the user's holdings; ensure_benchmark() adds
the row the first time a benchmark is chosen.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.db.models import (
    PRICE_SOURCE_YAHOO,
    AssetType,
    FxRate,
    InvestmentKind,
    InvestmentTransaction,
    PriceHistory,
    Security,
)
from app.investments.benchmarks import BENCHMARKS
from app.investments.market_data import fx_pair
from app.investments.portfolio import build_positions, split_factor_after
from app.investments.snapshot import PortfolioSnapshot, portfolio_snapshot


def ensure_benchmark(session: Session, key: str) -> Security:
    """The catalogue row of a benchmark, created (without network) if missing."""
    bench = BENCHMARKS[key]
    security = session.query(Security).filter(Security.symbol == bench.symbol).one_or_none()
    if security is None:
        security = Security(symbol=bench.symbol, name=bench.name, asset_type=AssetType.etf,
                            currency="EUR", exchange=bench.exchange,
                            price_source=PRICE_SOURCE_YAHOO)
        session.add(security)
        session.commit()
    return security


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
@dataclass
class HistoryPoint:
    day:           date
    value_eur:     float
    invested_eur:  float
    benchmark_eur: Optional[float] = None


@dataclass
class PortfolioHistory:
    points:          List[HistoryPoint] = field(default_factory=list)
    benchmark_name:  Optional[str] = None

    def since(self, start: Optional[date]) -> List[HistoryPoint]:
        if start is None:
            return list(self.points)
        return [p for p in self.points if p.day >= start]


class _Series:
    """Forward-filled lookup of a dated series (last value on or before a day)."""

    def __init__(self, rows: Sequence[Tuple[date, float]]):
        self._days = [d for d, _ in rows]
        self._values = [v for _, v in rows]

    def at(self, day: date) -> Optional[float]:
        index = bisect_right(self._days, day)
        return self._values[index - 1] if index else None

    def first_after(self, day: date) -> Optional[float]:
        index = bisect_right(self._days, day)
        return self._values[index] if index < len(self._values) else None

    @property
    def days(self) -> List[date]:
        return self._days


def _price_series(session: Session, security_ids, start: date, end: date) -> Dict[int, _Series]:
    rows = defaultdict(list)
    query = (session.query(PriceHistory.security_id, PriceHistory.price_date, PriceHistory.close)
             .filter(PriceHistory.security_id.in_(list(security_ids)),
                     PriceHistory.price_date <= end)
             .order_by(PriceHistory.price_date))
    for security_id, day, close in query:
        rows[security_id].append((day, close))
    return {sid: _Series(r) for sid, r in rows.items()}


def _fx_series(session: Session, currencies, end: date) -> Dict[str, Tuple[_Series, float]]:
    """currency → (series of units per EUR, minor-unit divisor)."""
    result = {}
    for currency in currencies:
        pair = fx_pair(currency)
        if pair is None:
            continue
        iso, divisor = pair
        rows = (session.query(FxRate.rate_date, FxRate.rate)
                .filter(FxRate.currency == iso, FxRate.rate_date <= end)
                .order_by(FxRate.rate_date).all())
        result[currency] = (_Series(rows), divisor)
    return result


def portfolio_history(
    session: Session,
    user_id: int,
    end: Optional[date] = None,
    broker: Optional[str] = None,
    benchmark_key: Optional[str] = None,
) -> PortfolioHistory:
    end = end or date.today()
    query = session.query(InvestmentTransaction).filter(
        InvestmentTransaction.user_id == user_id,
        InvestmentTransaction.trade_date <= end,
    )
    if broker:
        query = query.filter(InvestmentTransaction.broker == broker)
    transactions = sorted(query.all(), key=lambda t: (t.trade_date, t.id))
    history = PortfolioHistory()
    if not transactions:
        return history
    start = transactions[0].trade_date

    securities = {s.id: s for s in session.query(Security)
                  .filter(Security.id.in_({t.security_id for t in transactions}))}
    prices = _price_series(session, securities, start, end)
    fx = _fx_series(session, {s.currency for s in securities.values()}, end)

    bench_series: Optional[_Series] = None
    if benchmark_key:
        bench = BENCHMARKS[benchmark_key]
        bench_security = session.query(Security).filter(Security.symbol == bench.symbol).one_or_none()
        if bench_security is not None:
            bench_series = _price_series(session, [bench_security.id], start, end).get(bench_security.id)
            if bench_series is not None:
                history.benchmark_name = bench.name

    days = {start, end}
    for series in prices.values():
        days.update(d for d in series.days if start <= d <= end)
    days.update(t.trade_date for t in transactions)

    by_day: Dict[date, List[InvestmentTransaction]] = defaultdict(list)
    for txn in transactions:
        by_day[txn.trade_date].append(txn)

    applied: List[InvestmentTransaction] = []
    positions = {}
    bench_units = 0.0
    for day in sorted(days):
        todays = by_day.get(day)
        if todays:
            applied.extend(todays)
            positions = build_positions(applied)
            if bench_series is not None:
                bench_units = _apply_to_benchmark(bench_units, todays, bench_series, day)

        value = invested = 0.0
        for security_id, position in positions.items():
            qty = position.quantity
            if qty <= 1e-9:
                continue
            cost = position.cost_basis_eur
            invested += cost
            security = securities[security_id]
            series = prices.get(security_id)
            close = series.at(day) if series else None
            rate = 1.0
            if security.currency in fx:
                fx_values, divisor = fx[security.currency]
                raw = fx_values.at(day) or fx_values.first_after(day)
                rate = raw * divisor if raw else None
            if close is not None and rate:
                # Closes before a later split are split-adjusted by the provider.
                close *= split_factor_after(transactions, security_id, day)
                value += qty * close / rate
            else:
                value += cost

        bench_value = None
        if bench_series is not None:
            price = bench_series.at(day) or bench_series.first_after(day)
            bench_value = bench_units * price if price else None
        history.points.append(HistoryPoint(day, value, invested, bench_value))
    return history


def _apply_to_benchmark(units: float, todays, series: _Series, day: date) -> float:
    price = series.at(day) or series.first_after(day)
    if not price:
        return units
    for txn in todays:
        kind = InvestmentKind(txn.kind)
        if kind == InvestmentKind.buy:
            units += float(txn.amount_eur) / price
        elif kind == InvestmentKind.grant:
            paid = txn.cash_paid_eur if txn.cash_paid_eur is not None else txn.amount_eur
            units += float(paid or 0.0) / price
        elif kind in (InvestmentKind.sell, InvestmentKind.dividend):
            units = max(0.0, units - float(txn.amount_eur) / price)
    return units


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
ALLOCATION_DIMENSIONS = {
    "security":   "Por valor",
    "broker":     "Por bróker",
    "currency":   "Por divisa",
    "asset_type": "Por tipo de activo",
}
_ASSET_LABELS = {AssetType.stock: "Acciones", AssetType.etf: "ETF", AssetType.fund: "Fondos"}
CASH_LABEL = "Efectivo"


def allocation(
    session: Session,
    user_id: int,
    by: str,
    snapshot: Optional[PortfolioSnapshot] = None,
    as_of: Optional[date] = None,
) -> List[Tuple[str, float]]:
    """[(label, value EUR)] of the open positions plus the brokers' cash, largest first."""
    snapshot = snapshot or portfolio_snapshot(session, user_id, as_of=as_of)
    totals: Dict[str, float] = defaultdict(float)
    if by == "broker":
        brokers = {b for row in snapshot.open_rows for b in row.brokers}
        for broker in brokers:
            per_broker = portfolio_snapshot(session, user_id, as_of=snapshot.as_of, broker=broker)
            for row in per_broker.open_rows:
                totals[broker] += row.market_value_eur
    else:
        for row in snapshot.open_rows:
            security = row.security
            if by == "security":
                label = security.name
            elif by == "currency":
                pair = fx_pair(security.currency)
                label = pair[0] if pair else "EUR"
            elif by == "asset_type":
                label = _ASSET_LABELS.get(AssetType(security.asset_type), "Otros")
            else:
                raise ValueError(f"Dimensión desconocida: {by}")
            totals[label] += row.market_value_eur
    # Uninvested cash is part of the capital too (one slice, or by its currency/broker).
    for line in snapshot.totals.cash:
        if line.amount_eur is None or line.amount_eur <= 0:
            continue
        label = {"currency": line.currency, "broker": line.broker}.get(by, CASH_LABEL)
        totals[label] += line.amount_eur
    return sorted(((k, v) for k, v in totals.items() if v > 0), key=lambda kv: -kv[1])
