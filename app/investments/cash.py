"""
app/investments/cash.py
=======================
FAMILYFIN — Uninvested cash at each broker (table broker_cash).

A broker account is worth its positions plus the cash not invested yet
(DEGIRO calls it «Margen libre»; «Cuenta completa» = cartera + margen libre).
The cash is not rebuilt from deposits and withdrawals: the importer stores
the newest «Saldo» of each currency the statement shows, as of its last day.

Rules
  * one row per (user, broker, currency);
  * an older statement never overwrites a newer balance (importing last
    month's file after this month's keeps this month's cash);
  * EUR value with the cached FX rate (prices.fx_rate); a currency without a
    rate is left out of the total and reported, like a missing price.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.db.models import BrokerCash
from app.investments.prices import fx_rate

SOURCE_STATEMENT = "extracto"


@dataclass
class CashLine:
    broker:     str
    currency:   str
    amount:     float                 # in `currency`
    amount_eur: Optional[float]       # None when there is no FX rate
    as_of:      date


def cash_changes(session: Session, user_id: int, broker: str, balances: Dict[str, float],
                 as_of: Optional[date]) -> Dict[str, Tuple[Optional[float], float]]:
    """currency → (stored amount or None, statement amount) for the balances a
    statement would change. Empty when nothing changes or it is older."""
    if not balances or as_of is None:
        return {}
    stored = {c.currency: c for c in session.query(BrokerCash).filter(
        BrokerCash.user_id == user_id, BrokerCash.broker == broker)}
    changes = {}
    for currency, amount in balances.items():
        row = stored.get(currency)
        if row is None:
            if abs(amount) >= 0.005:
                changes[currency] = (None, amount)
        elif as_of >= row.as_of and abs(row.amount - amount) >= 0.005:
            changes[currency] = (row.amount, amount)
    return changes


def save_statement_cash(session: Session, user_id: int, broker: str,
                        balances: Dict[str, float], as_of: Optional[date]) -> bool:
    """Store a statement's balances unless a newer one is already stored.
    Commits. True when something was written."""
    if not balances or as_of is None:
        return False
    stored = {c.currency: c for c in session.query(BrokerCash).filter(
        BrokerCash.user_id == user_id, BrokerCash.broker == broker)}
    if any(row.as_of > as_of for row in stored.values()):
        return False                  # an older statement: keep the newer cash
    written = False
    for currency, amount in balances.items():
        row = stored.get(currency)
        if row is None:
            session.add(BrokerCash(user_id=user_id, broker=broker, currency=currency,
                                   amount=round(amount, 2), as_of=as_of, source=SOURCE_STATEMENT))
            written = True
        elif row.as_of != as_of or abs(row.amount - amount) >= 0.005:
            row.amount, row.as_of, row.source = round(amount, 2), as_of, SOURCE_STATEMENT
            written = True
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return written


def user_cash(session: Session, user_id: int, broker: Optional[str] = None,
              on: Optional[date] = None) -> List[CashLine]:
    """The user's non-zero balances (optionally of one broker), valued in EUR."""
    query = session.query(BrokerCash).filter(BrokerCash.user_id == user_id)
    if broker:
        query = query.filter(BrokerCash.broker == broker)
    lines = []
    for row in query.order_by(BrokerCash.broker, BrokerCash.currency):
        if abs(row.amount) < 0.005:
            continue
        if row.currency == "EUR":
            amount_eur = row.amount
        else:
            rate = fx_rate(session, row.currency, on=on)
            amount_eur = round(row.amount / rate, 2) if rate else None
        lines.append(CashLine(row.broker, row.currency, row.amount, amount_eur, row.as_of))
    return lines
