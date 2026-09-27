"""
app/investments/returns.py
==========================
FAMILYFIN — Money-weighted return (XIRR) of a user's portfolio.

Cash flows are seen from the investor's pocket: EUR paid out is negative
(buys, the cash part of employer grants), EUR received is positive (sales,
net dividends), and the current market value closes the series as if the
portfolio were sold on `as_of`. XIRR is the annual rate that makes the
present value of those flows zero, i.e. it accounts for when each euro was
invested, which a plain "gain / cost" percentage does not.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, List, Optional, Tuple

from app.db.models import InvestmentKind

CashFlow = Tuple[date, float]


def investor_cash_flows(
    transactions: Iterable,
    market_value_eur: float,
    as_of: date,
) -> List[CashFlow]:
    """Cash flows of the ledger plus the closing market value on as_of."""
    flows: List[CashFlow] = []
    for txn in transactions:
        kind = InvestmentKind(txn.kind)
        if kind == InvestmentKind.buy:
            flows.append((txn.trade_date, -float(txn.amount_eur)))
        elif kind == InvestmentKind.grant:
            paid = txn.cash_paid_eur if txn.cash_paid_eur is not None else txn.amount_eur
            if paid:
                flows.append((txn.trade_date, -float(paid)))
        elif kind in (InvestmentKind.sell, InvestmentKind.dividend):
            flows.append((txn.trade_date, float(txn.amount_eur)))
    if market_value_eur:
        flows.append((as_of, float(market_value_eur)))
    return sorted(flows)


def _npv(rate: float, flows: List[CashFlow], start: date) -> float:
    return sum(
        amount / (1.0 + rate) ** ((when - start).days / 365.0)
        for when, amount in flows
    )


def xirr(flows: Iterable[CashFlow], tolerance: float = 1e-9) -> Optional[float]:
    """Annualized internal rate of return of dated cash flows.

    Returns None when it is undefined: fewer than two flows, all flows with
    the same sign, or no root between -99.99 % and +10,000 % a year.
    """
    flows = sorted((when, amount) for when, amount in flows if amount)
    if len(flows) < 2:
        return None
    if all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return None
    start = flows[0][0]

    # NPV decreases with the rate for "pay first, receive later" series, but
    # not always monotonically; bisection on a sign change is robust.
    low, high = -0.9999, 100.0
    npv_low, npv_high = _npv(low, flows, start), _npv(high, flows, start)
    if npv_low * npv_high > 0:
        return None
    for _ in range(300):
        mid = (low + high) / 2.0
        npv_mid = _npv(mid, flows, start)
        if abs(npv_mid) < tolerance or (high - low) / 2.0 < tolerance:
            return mid
        if npv_low * npv_mid < 0:
            high, npv_high = mid, npv_mid
        else:
            low, npv_low = mid, npv_mid
    return (low + high) / 2.0
