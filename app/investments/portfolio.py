"""
app/investments/portfolio.py
============================
FAMILYFIN — Positions, FIFO lots and realized gains derived from the ledger.

Nothing here touches the database or the network: the functions take any
objects shaped like InvestmentTransaction (ORM rows or plain dataclasses)
and return plain dataclasses, so the GUI, reports and tests share one
implementation.

Rules
-----
* Holdings are never stored; they are replayed from the trades in date
  order. On one day a split applies first, then buys/grants, then sells,
  then dividends (a same-day buy+sell works; a split re-prices the day).
* Sales consume the oldest lots first (FIFO), as Spanish IRPF requires for
  homogeneous securities. Each consumed lot yields one RealizedGain row with
  its acquisition date and value, the shape of the tax return's
  "ganancias patrimoniales" section. Sale proceeds are split across lots in
  proportion to the shares taken.
* Two cost figures travel with every lot: `cost_eur` (tax basis) and
  `cash_eur` (EUR actually paid). They differ only for employer-plan grants.
* Selling more shares than held on that date is a LedgerError.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional

from app.db.models import InvestmentKind

# Shares below this are treated as zero (fund units carry 6 decimals and
# floats accumulate noise when lots are split).
QTY_TOLERANCE = 1e-6

# Replay order of kinds within the same trade date.
_KIND_ORDER = {
    InvestmentKind.split:    0,
    InvestmentKind.buy:      1,
    InvestmentKind.grant:    1,
    InvestmentKind.sell:     2,
    InvestmentKind.dividend: 3,
}


class LedgerError(ValueError):
    """An operation is impossible given the ones before it. Message in Spanish."""

    def __init__(self, message: str, transaction=None):
        super().__init__(message)
        self.transaction = transaction


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class Lot:
    """Shares still held from one acquisition."""
    acquired:       date
    quantity:       float
    cost_eur:       float           # remaining tax cost basis
    cash_eur:       float           # remaining EUR actually paid
    transaction_id: Optional[int] = None


@dataclass
class RealizedGain:
    """Shares of one lot disposed of by one sale."""
    security_id:         int
    sell_date:           date
    acquired:            date
    quantity:            float
    proceeds_eur:        float
    cost_eur:            float
    sell_transaction_id: Optional[int] = None

    @property
    def gain_eur(self) -> float:
        return self.proceeds_eur - self.cost_eur


@dataclass
class Position:
    """Everything the ledger says about one security of one user."""
    security_id:            int
    lots:                   List[Lot] = field(default_factory=list)
    realized:               List[RealizedGain] = field(default_factory=list)
    dividends_eur:          float = 0.0     # net received
    dividends_withheld_eur: float = 0.0
    fees_eur:               float = 0.0

    @property
    def quantity(self) -> float:
        return sum(lot.quantity for lot in self.lots)

    @property
    def is_open(self) -> bool:
        return self.quantity > QTY_TOLERANCE

    @property
    def cost_basis_eur(self) -> float:
        return sum(lot.cost_eur for lot in self.lots)

    @property
    def cash_invested_eur(self) -> float:
        return sum(lot.cash_eur for lot in self.lots)

    @property
    def average_cost_eur(self) -> Optional[float]:
        qty = self.quantity
        return self.cost_basis_eur / qty if qty > QTY_TOLERANCE else None

    @property
    def realized_gain_eur(self) -> float:
        return sum(r.gain_eur for r in self.realized)


@dataclass
class PositionValue:
    """A position priced at market."""
    security_id:          int
    quantity:             float
    cost_basis_eur:       float
    price:                float             # in the security's currency
    fx_rate:              float             # units of that currency per EUR
    market_value_eur:     float
    unrealized_gain_eur:  float
    unrealized_pct:       Optional[float]   # None when cost is zero
    day_change_eur:       Optional[float]   # None without a previous close


@dataclass
class YearRealized:
    """Tax-year totals (sale date decides the year)."""
    year:              int
    proceeds_eur:      float = 0.0
    cost_eur:          float = 0.0
    gains_eur:         float = 0.0      # sum of positive lot results
    losses_eur:        float = 0.0      # sum of negative lot results (<= 0)
    dividends_eur:     float = 0.0      # net
    withheld_eur:      float = 0.0

    @property
    def net_gain_eur(self) -> float:
        return self.gains_eur + self.losses_eur


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------
def _kind(txn) -> InvestmentKind:
    return InvestmentKind(txn.kind)


def _fmt_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _fmt_qty(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", ",")


def validate_fields(txn) -> List[str]:
    """Problems with a single operation, independent of the others (Spanish)."""
    errors: List[str] = []
    kind = _kind(txn)
    if txn.trade_date is None:
        errors.append("La fecha es obligatoria.")
    if kind in (InvestmentKind.buy, InvestmentKind.sell, InvestmentKind.grant):
        if not txn.quantity or txn.quantity <= 0:
            errors.append("El número de acciones debe ser mayor que cero.")
        if txn.amount_eur is None or txn.amount_eur < 0:
            errors.append("El importe en EUR no puede ser negativo.")
        elif kind != InvestmentKind.grant and txn.amount_eur == 0:
            errors.append("El importe en EUR debe ser mayor que cero.")
    if kind == InvestmentKind.grant and txn.cash_paid_eur is not None and txn.cash_paid_eur < 0:
        errors.append("El importe pagado no puede ser negativo.")
    if kind == InvestmentKind.dividend and (txn.amount_eur is None or txn.amount_eur <= 0):
        errors.append("El dividendo neto debe ser mayor que cero.")
    if kind == InvestmentKind.split and (
        not txn.split_ratio or txn.split_ratio <= 0 or txn.split_ratio == 1
    ):
        errors.append("La proporción del split debe ser positiva y distinta de 1.")
    if (txn.fees_eur or 0) < 0 or (txn.tax_withheld_eur or 0) < 0:
        errors.append("Comisiones y retenciones no pueden ser negativas.")
    return errors


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def _sort_key(txn):
    return (txn.trade_date, _KIND_ORDER[_kind(txn)], txn.id or 0)


def _sell(position: Position, txn) -> None:
    to_sell = float(txn.quantity)
    held = position.quantity
    if to_sell > held + QTY_TOLERANCE:
        raise LedgerError(
            f"La venta del {_fmt_date(txn.trade_date)} es de {_fmt_qty(to_sell)} "
            f"acciones, pero en esa fecha solo había {_fmt_qty(held)}.",
            txn,
        )
    proceeds_total = float(txn.amount_eur)
    remaining = to_sell
    while remaining > QTY_TOLERANCE and position.lots:
        lot = position.lots[0]
        take = min(lot.quantity, remaining)
        share = take / lot.quantity
        cost = lot.cost_eur * share
        cash = lot.cash_eur * share
        position.realized.append(RealizedGain(
            security_id=position.security_id,
            sell_date=txn.trade_date,
            acquired=lot.acquired,
            quantity=take,
            proceeds_eur=proceeds_total * take / to_sell,
            cost_eur=cost,
            sell_transaction_id=txn.id,
        ))
        lot.quantity -= take
        lot.cost_eur -= cost
        lot.cash_eur -= cash
        remaining -= take
        if lot.quantity <= QTY_TOLERANCE:
            position.lots.pop(0)


def build_positions(transactions: Iterable) -> Dict[int, Position]:
    """Replay a user's ledger into one Position per security_id.

    Raises LedgerError for the first invalid or impossible operation.
    """
    positions: Dict[int, Position] = {}
    for txn in sorted(transactions, key=_sort_key):
        errors = validate_fields(txn)
        if errors:
            raise LedgerError(
                f"Operación del {_fmt_date(txn.trade_date)}: {errors[0]}", txn
            )
        position = positions.setdefault(txn.security_id, Position(txn.security_id))
        kind = _kind(txn)
        position.fees_eur += txn.fees_eur or 0.0

        if kind in (InvestmentKind.buy, InvestmentKind.grant):
            cost = float(txn.amount_eur)
            cash = cost
            if kind == InvestmentKind.grant and txn.cash_paid_eur is not None:
                cash = float(txn.cash_paid_eur)
            position.lots.append(Lot(
                acquired=txn.trade_date,
                quantity=float(txn.quantity),
                cost_eur=cost,
                cash_eur=cash,
                transaction_id=txn.id,
            ))
        elif kind == InvestmentKind.sell:
            _sell(position, txn)
        elif kind == InvestmentKind.split:
            for lot in position.lots:
                lot.quantity *= float(txn.split_ratio)
        elif kind == InvestmentKind.dividend:
            position.dividends_eur += float(txn.amount_eur)
            position.dividends_withheld_eur += txn.tax_withheld_eur or 0.0
    return positions


def split_factor_after(transactions: Iterable, security_id: int, day: date) -> float:
    """Product of the ratios of this security's splits dated after `day`.

    Provider closes are split-adjusted: a close from before a 2-for-1 split
    is half the price actually paid that day. Multiplying such a close by
    this factor gives the real historical price, and pre-split shares × the
    adjusted close × factor gives the real value.
    """
    factor = 1.0
    for txn in transactions:
        if (txn.security_id == security_id and _kind(txn) == InvestmentKind.split
                and txn.trade_date > day and txn.split_ratio):
            factor *= float(txn.split_ratio)
    return factor


def ledger_error(transactions: Iterable) -> Optional[LedgerError]:
    """The LedgerError the ledger would raise, or None if it is consistent.

    The entry form uses this on "existing trades + the new/edited one" so an
    edit that makes a later sale impossible is rejected too.
    """
    try:
        build_positions(transactions)
    except LedgerError as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# Valuation and reports
# ---------------------------------------------------------------------------
def value_position(
    position: Position,
    price: float,
    fx_rate: float = 1.0,
    previous_price: Optional[float] = None,
    previous_fx_rate: Optional[float] = None,
) -> PositionValue:
    """Price a position. fx_rate = units of the quote currency per EUR (1 for EUR)."""
    if fx_rate <= 0:
        raise ValueError("fx_rate debe ser positivo")
    qty = position.quantity
    market_value = qty * price / fx_rate
    cost = position.cost_basis_eur
    gain = market_value - cost
    day_change = None
    if previous_price is not None:
        prev_fx = previous_fx_rate or fx_rate
        day_change = market_value - qty * previous_price / prev_fx
    return PositionValue(
        security_id=position.security_id,
        quantity=qty,
        cost_basis_eur=cost,
        price=price,
        fx_rate=fx_rate,
        market_value_eur=market_value,
        unrealized_gain_eur=gain,
        unrealized_pct=(gain / cost) if cost > 0 else None,
        day_change_eur=day_change,
    )


def realized_by_year(transactions: Iterable) -> Dict[int, YearRealized]:
    """Realized FIFO results and dividends grouped by calendar (tax) year."""
    transactions = list(transactions)
    years: Dict[int, YearRealized] = defaultdict(lambda: YearRealized(0))
    for position in build_positions(transactions).values():
        for realized in position.realized:
            year = years[realized.sell_date.year]
            year.proceeds_eur += realized.proceeds_eur
            year.cost_eur += realized.cost_eur
            if realized.gain_eur >= 0:
                year.gains_eur += realized.gain_eur
            else:
                year.losses_eur += realized.gain_eur
    for txn in transactions:
        if _kind(txn) == InvestmentKind.dividend:
            year = years[txn.trade_date.year]
            year.dividends_eur += float(txn.amount_eur)
            year.withheld_eur += txn.tax_withheld_eur or 0.0
    result = {}
    for number in sorted(years):
        entry = years[number]
        entry.year = number
        result[number] = entry
    return result
