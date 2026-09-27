"""
app/investments/prices.py
=========================
FAMILYFIN — Local cache of prices and FX rates, and the refresh that fills it.

The views only ever read from SQLite (price_history, fx_rates), so the
portfolio opens instantly and works offline; refresh_market_data() tops the
cache up from the provider and is meant to run in a background thread with
its own Session (see refresh_worker.py).

Refresh window per security:
  * nothing cached yet → from its first trade (any user) minus a week, so the
    evolution chart and "value on the trade date" have data;
  * otherwise → from the last cached day minus OVERLAP_DAYS, which also
    replaces today's intraday bar with the final close.
Prices typed in by hand (source 'manual') are never overwritten.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import (
    PRICE_SOURCE_MANUAL,
    PRICE_SOURCE_YAHOO,
    FxRate,
    InvestmentTransaction,
    PriceHistory,
    Security,
)
from app.investments.benchmarks import BENCHMARK_SYMBOLS
from app.investments.market_data import (
    MarketDataError,
    MarketDataProvider,
    SecurityInfo,
    fx_pair,
    fx_symbol,
)

logger = logging.getLogger(__name__)

OVERLAP_DAYS = 5
HISTORY_MARGIN_DAYS = 7
STALE_AFTER = timedelta(minutes=15)


@dataclass
class RefreshResult:
    started_at:    datetime
    finished_at:   Optional[datetime] = None
    updated:       List[str] = field(default_factory=list)      # symbols with new data
    missing:       List[str] = field(default_factory=list)      # symbols the provider did not return
    price_rows:    int = 0
    fx_rows:       int = 0
    error:         Optional[str] = None                         # whole refresh failed (Spanish)

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class LatestPrice:
    security_id:    int
    price_date:     date
    close:          float
    source:         str
    previous_date:  Optional[date] = None
    previous_close: Optional[float] = None


def is_stale(last_refresh: Optional[datetime], now: Optional[datetime] = None,
             max_age: timedelta = STALE_AFTER) -> bool:
    """Whether prices refreshed at last_refresh should be refreshed again."""
    if last_refresh is None:
        return True
    return (now or datetime.now()) - last_refresh >= max_age


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------
def _securities_to_refresh(session: Session) -> List[Security]:
    """Provider-priced securities in anyone's ledger, plus catalogued benchmarks.

    Benchmarks are only worth downloading once somebody has operations.
    """
    held = session.query(InvestmentTransaction.security_id)
    if session.query(held.exists()).scalar() is not True:
        return []
    return (
        session.query(Security)
        .filter(
            Security.price_source != PRICE_SOURCE_MANUAL,
            Security.id.in_(held) | Security.symbol.in_(BENCHMARK_SYMBOLS),
        )
        .order_by(Security.symbol)
        .all()
    )


def _refresh_start(session: Session, security: Security, today: date) -> date:
    last_cached = (
        session.query(func.max(PriceHistory.price_date))
        .filter(
            PriceHistory.security_id == security.id,
            PriceHistory.source == PRICE_SOURCE_YAHOO,
        )
        .scalar()
    )
    if last_cached is not None:
        return min(last_cached, today) - timedelta(days=OVERLAP_DAYS)
    first_trade = (
        session.query(func.min(InvestmentTransaction.trade_date))
        .filter(InvestmentTransaction.security_id == security.id)
        .scalar()
    )
    if first_trade is None:     # a benchmark: cover everybody's history
        first_trade = session.query(func.min(InvestmentTransaction.trade_date)).scalar()
    first_trade = first_trade or today
    return first_trade - timedelta(days=HISTORY_MARGIN_DAYS)


def _chunks(values: list, size: int = 500):
    """Multi-row INSERTs in slices, well under SQLite's bound-variable limit."""
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _upsert_prices(session: Session, security_id: int, rows, start: date) -> int:
    values = [
        {"security_id": security_id, "price_date": day, "close": close,
         "source": PRICE_SOURCE_YAHOO}
        for day, close in rows if day >= start
    ]
    for chunk in _chunks(values):
        stmt = sqlite_insert(PriceHistory).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[PriceHistory.security_id, PriceHistory.price_date],
            set_={"close": stmt.excluded.close, "source": stmt.excluded.source},
            where=PriceHistory.source != PRICE_SOURCE_MANUAL,
        )
        session.execute(stmt)
    return len(values)


def _upsert_fx(session: Session, currency: str, rows) -> int:
    values = [{"currency": currency, "rate_date": day, "rate": rate} for day, rate in rows]
    for chunk in _chunks(values):
        stmt = sqlite_insert(FxRate).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FxRate.currency, FxRate.rate_date],
            set_={"rate": stmt.excluded.rate},
        )
        session.execute(stmt)
    return len(values)


def refresh_market_data(
    session: Session,
    provider: MarketDataProvider,
    today: Optional[date] = None,
) -> RefreshResult:
    """Download missing prices and FX rates into the cache and commit.

    Never raises for provider problems: the cache keeps its previous content
    and RefreshResult.error carries a Spanish message for the status bar.
    """
    today = today or date.today()
    result = RefreshResult(started_at=datetime.now())
    securities = _securities_to_refresh(session)
    if not securities:
        result.finished_at = datetime.now()
        return result

    starts = {s.symbol: _refresh_start(session, s, today) for s in securities}
    fx_starts: Dict[str, date] = {}
    for security in securities:
        pair = fx_pair(security.currency)
        if pair:
            iso = pair[0]
            fx_starts[iso] = min(fx_starts.get(iso, today), starts[security.symbol])

    symbols = list(starts) + [fx_symbol(iso) for iso in fx_starts]
    earliest = min(list(starts.values()) + list(fx_starts.values()))
    try:
        closes = provider.daily_closes(symbols, start=earliest)
    except MarketDataError as exc:
        result.error = str(exc)
        result.finished_at = datetime.now()
        return result

    try:
        for security in securities:
            rows = closes.get(security.symbol)
            if not rows:
                result.missing.append(security.symbol)
                continue
            written = _upsert_prices(session, security.id, rows, starts[security.symbol])
            if written:
                result.updated.append(security.symbol)
                result.price_rows += written
        for iso, start in fx_starts.items():
            rows = [(d, r) for d, r in closes.get(fx_symbol(iso), []) if d >= start]
            if not rows:
                result.missing.append(fx_symbol(iso))
            result.fx_rows += _upsert_fx(session, iso, rows)
        session.commit()
    except Exception:
        session.rollback()
        raise
    result.finished_at = datetime.now()
    logger.info(
        "Cotizaciones actualizadas: %d valores, %d precios, %d cambios; sin datos: %s",
        len(result.updated), result.price_rows, result.fx_rows, result.missing or "-",
    )
    return result


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def latest_prices(
    session: Session, security_ids: Iterable[int], as_of: Optional[date] = None
) -> Dict[int, LatestPrice]:
    """Most recent close (and the one before, for the day change) per security."""
    result: Dict[int, LatestPrice] = {}
    for security_id in set(security_ids):
        query = session.query(PriceHistory).filter(PriceHistory.security_id == security_id)
        if as_of is not None:
            query = query.filter(PriceHistory.price_date <= as_of)
        rows = query.order_by(PriceHistory.price_date.desc()).limit(2).all()
        if not rows:
            continue
        last = rows[0]
        previous = rows[1] if len(rows) > 1 else None
        result[security_id] = LatestPrice(
            security_id=security_id,
            price_date=last.price_date,
            close=last.close,
            source=last.source,
            previous_date=previous.price_date if previous else None,
            previous_close=previous.close if previous else None,
        )
    return result


def close_on(session: Session, security_id: int, day: date) -> Optional[LatestPrice]:
    """The cached close on `day`, or the last one before it (weekends, holidays)."""
    return latest_prices(session, [security_id], as_of=day).get(security_id)


def fx_rate(session: Session, currency: str, on: Optional[date] = None) -> Optional[float]:
    """Units of `currency` per EUR on (or before) a day; 1.0 for EUR.

    Minor-unit quote currencies (GBp) include their ×100 factor, so a price
    divided by this rate is always EUR. None when no rate is cached yet.
    """
    pair = fx_pair(currency)
    if pair is None:
        return 1.0
    iso, divisor = pair
    query = session.query(FxRate.rate).filter(FxRate.currency == iso)
    if on is not None:
        query = query.filter(FxRate.rate_date <= on)
    rate = query.order_by(FxRate.rate_date.desc()).limit(1).scalar()
    return rate * divisor if rate is not None else None


def set_manual_price(session: Session, security_id: int, day: date, close: float) -> None:
    """Store a hand-typed price (e.g. a fund NAV); refreshes never overwrite it."""
    if close <= 0:
        raise ValueError("El precio debe ser mayor que cero.")
    stmt = sqlite_insert(PriceHistory).values(
        security_id=security_id, price_date=day, close=close, source=PRICE_SOURCE_MANUAL
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PriceHistory.security_id, PriceHistory.price_date],
        set_={"close": stmt.excluded.close, "source": PRICE_SOURCE_MANUAL},
    )
    session.execute(stmt)
    session.commit()


# ---------------------------------------------------------------------------
# Securities catalogue
# ---------------------------------------------------------------------------
def find_or_create_security(
    session: Session,
    provider: MarketDataProvider,
    symbol: str,
    isin: Optional[str] = None,
) -> Security:
    """The catalogue row for a provider symbol, creating it on first use.

    Creating it costs one provider call (name, currency, exchange, sector,
    country). Raises MarketDataError when the provider does not know it.
    Does not commit: the caller saves it together with the first trade.
    """
    symbol = symbol.strip().upper()
    security = session.query(Security).filter(Security.symbol == symbol).one_or_none()
    if security is not None:
        if isin and not security.isin:
            security.isin = isin.strip().upper()
        return security
    return security_from_info(session, provider.security_info(symbol), isin)


def security_from_info(
    session: Session, info: SecurityInfo, isin: Optional[str] = None
) -> Security:
    """Catalogue row for provider data already fetched (e.g. in a worker thread).

    Returns the existing row when the symbol is already catalogued. Flushes,
    does not commit.
    """
    symbol = info.symbol.strip().upper()
    security = session.query(Security).filter(Security.symbol == symbol).one_or_none()
    if security is None:
        security = Security(
            symbol=symbol,
            name=info.name,
            asset_type=info.asset_type,
            currency=info.currency,
            exchange=info.exchange,
            sector=info.sector,
            country=info.country,
            price_source=PRICE_SOURCE_YAHOO,
        )
        session.add(security)
    if isin and not security.isin:
        security.isin = isin.strip().upper()
    session.flush()
    return security
