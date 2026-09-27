"""
app/investments/events.py
=========================
FAMILYFIN — Dividends and splits the provider knows about but the user has
not registered yet.

fetch_corporate_actions() is the network part (run it in a worker thread);
pending_events() is pure and compares those events with the user's ledger:

  * a dividend is pending when the user held shares the day before its
    ex-date and has no dividend of that security registered between
    DIVIDEND_MATCH_BEFORE days before and DIVIDEND_MATCH_AFTER days after
    it (payment usually comes days or weeks after the ex-date);
  * a split is pending when shares were held on the split date and no split
    of that security is registered within SPLIT_MATCH_DAYS of it.

The estimate for a dividend is gross: shares × amount per share ÷ FX of
the ex-date. The net amount and the withholding come from the broker
statement; the user types them when registering it.

A dismissed event is remembered by its key ("dividend|GEV|2026-03-17").
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from app.db.models import InvestmentKind, InvestmentTransaction, Security
from app.investments.market_data import CorporateActions, MarketDataError, MarketDataProvider
from app.investments.portfolio import LedgerError, build_positions
from app.investments.prices import fx_rate

logger = logging.getLogger(__name__)

DIVIDEND_MATCH_BEFORE = 5
DIVIDEND_MATCH_AFTER = 60
SPLIT_MATCH_DAYS = 7


@dataclass(frozen=True)
class PendingEvent:
    kind:               InvestmentKind          # dividend | split
    security_id:        int
    symbol:             str
    name:               str
    ex_date:            date
    shares:             float                   # held the day before the ex-date
    per_share:          Optional[float] = None  # dividend, quote currency
    currency:           Optional[str] = None
    gross_eur:          Optional[float] = None  # dividend estimate (None without FX)
    ratio:              Optional[float] = None  # split

    @property
    def key(self) -> str:
        return f"{self.kind.value}|{self.symbol}|{self.ex_date.isoformat()}"


def fetch_corporate_actions(
    provider: MarketDataProvider, symbols: Iterable[str]
) -> Dict[str, CorporateActions]:
    """Provider data for each symbol; symbols that fail are skipped (logged)."""
    result: Dict[str, CorporateActions] = {}
    for symbol in sorted(set(symbols)):
        try:
            result[symbol] = provider.corporate_actions(symbol)
        except MarketDataError as exc:
            logger.info("Sin dividendos/splits para %s: %s", symbol, exc)
    return result


def held_symbols(session: Session, user_id: int) -> List[str]:
    return [s for (s,) in session.query(Security.symbol)
            .join(InvestmentTransaction, InvestmentTransaction.security_id == Security.id)
            .filter(InvestmentTransaction.user_id == user_id).distinct()]


def _shares_before(ledger: List[InvestmentTransaction], security_id: int, day: date) -> float:
    rows = [t for t in ledger if t.security_id == security_id and t.trade_date < day]
    try:
        position = build_positions(rows).get(security_id)
    except LedgerError:
        return 0.0
    return position.quantity if position else 0.0


def pending_events(
    session: Session,
    user_id: int,
    actions: Dict[str, CorporateActions],
    dismissed: Optional[Set[str]] = None,
    today: Optional[date] = None,
) -> List[PendingEvent]:
    today = today or date.today()
    dismissed = dismissed or set()
    ledger = (session.query(InvestmentTransaction)
              .filter(InvestmentTransaction.user_id == user_id).all())
    by_symbol = {s.symbol: s for s in session.query(Security)
                 .filter(Security.id.in_({t.security_id for t in ledger}))}
    registered: Dict[tuple, List[date]] = {}
    for txn in ledger:
        registered.setdefault((txn.security_id, InvestmentKind(txn.kind)), []).append(txn.trade_date)

    events: List[PendingEvent] = []
    for symbol, acts in actions.items():
        security = by_symbol.get(symbol)
        if security is None:
            continue
        first_trade = min(t.trade_date for t in ledger if t.security_id == security.id)
        dividend_dates = registered.get((security.id, InvestmentKind.dividend), [])
        split_dates = registered.get((security.id, InvestmentKind.split), [])

        for ex_date, per_share in acts.dividends:
            if ex_date <= first_trade or ex_date > today:
                continue
            if any(ex_date - timedelta(days=DIVIDEND_MATCH_BEFORE) <= d
                   <= ex_date + timedelta(days=DIVIDEND_MATCH_AFTER) for d in dividend_dates):
                continue
            shares = _shares_before(ledger, security.id, ex_date)
            if shares <= 1e-6:
                continue
            rate = fx_rate(session, security.currency, on=ex_date)
            events.append(PendingEvent(
                kind=InvestmentKind.dividend, security_id=security.id, symbol=symbol,
                name=security.name, ex_date=ex_date, shares=shares, per_share=per_share,
                currency=security.currency,
                gross_eur=round(shares * per_share / rate, 2) if rate else None,
            ))

        for split_date, ratio in acts.splits:
            if split_date <= first_trade or split_date > today:
                continue
            if any(abs((d - split_date).days) <= SPLIT_MATCH_DAYS for d in split_dates):
                continue
            shares = _shares_before(ledger, security.id, split_date)
            if shares <= 1e-6:
                continue
            events.append(PendingEvent(
                kind=InvestmentKind.split, security_id=security.id, symbol=symbol,
                name=security.name, ex_date=split_date, shares=shares, ratio=ratio,
            ))

    events = [e for e in events if e.key not in dismissed]
    # Splits first: they change the shares every later dividend is based on.
    return sorted(events, key=lambda e: (e.kind != InvestmentKind.split, e.ex_date))
