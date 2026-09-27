"""
app/investments/importers/symbols.py
====================================
FAMILYFIN — Find the market-data symbol of each security in a statement.

Broker statements give an ISIN, a name and the trade currency, but not the
exchange: DEGIRO buys US companies on Tradegate/Xetra in EUR, and Yahoo's
ISIN search usually returns the home (USD) listing. So, per ISIN:

  1. a security already in the catalogue with that ISIN wins (no network);
  2. otherwise candidates come from searching the ISIN and the name, kept
     when their Yahoo suffix matches the trade currency (.DE/.F/.AS… → EUR);
  3. candidates are priced on the trade dates (one batched download) and the
     one whose close is closest to the prices paid is proposed, provided it
     is within MAX_PRICE_GAP; ties prefer the main venues (Xetra, Euronext,
     Madrid, Milan) over regional German exchanges.

The user sees the proposal and the alternatives in the import preview and
can type any other symbol.

suggest_symbols() calls the provider: run it in a worker thread.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple

from app.investments.market_data import MarketDataError, MarketDataProvider

logger = logging.getLogger(__name__)

MAX_PRICE_GAP = 0.15

_EUR_SUFFIXES = {"DE", "F", "SG", "DU", "MU", "BE", "HM", "HA", "AS", "PA", "BR", "MC",
                 "MI", "VI", "LS", "IR", "HE"}
_SUFFIX_CURRENCY = {**{s: "EUR" for s in _EUR_SUFFIXES}, "L": "GBP", "SW": "CHF", "": "USD"}
# Lower is preferred when two listings price equally well.
_VENUE_RANK = {"DE": 0, "AS": 1, "PA": 1, "MC": 1, "MI": 1, "BR": 2, "F": 3, "L": 3}


@dataclass
class Candidate:
    symbol:   str
    exchange: str
    name:     str
    gap:      Optional[float] = None       # median (close / price paid − 1) on trade days

    @property
    def gap_text(self) -> str:
        if self.gap is None:
            return "sin cotización"
        return f"{self.gap * 100:+.1f} %".replace(".", ",").replace("-", "−")


@dataclass
class SymbolChoice:
    isin:        str
    name:        str
    currency:    str
    chosen:      Optional[str] = None
    candidates:  List[Candidate] = field(default_factory=list)
    from_catalogue: bool = False


def _suffix(symbol: str) -> str:
    return symbol.rsplit(".", 1)[1] if "." in symbol else ""


def _rank(candidate: "Candidate") -> int:
    return _VENUE_RANK.get(_suffix(candidate.symbol), 4)


def _root(symbol: str) -> str:
    return symbol.rsplit(".", 1)[0]


def listing_currency(symbol: str) -> Optional[str]:
    return _SUFFIX_CURRENCY.get(_suffix(symbol))


_NOISE = re.compile(
    r"[,.]?\s+(Inc|Corp|Corporation|SA|AG|SE|NV|plc|Ltd|Co)\.?$"
    r"|\s+(USD|EUR)\s+(Dis|Acc|A|D)$|\s+UCITS ETF.*$", re.IGNORECASE)


def search_queries(isin: str, info: dict) -> List[str]:
    """ISIN first, then name variants Yahoo's search matches better.

    Yahoo finds "Take-Two Interactive Software" but not "... Software, Inc.",
    and DEGIRO's truncated upper-case product names work surprisingly well.
    """
    name = info.get("name") or ""
    queries = [isin, info.get("product") or "", name]
    cleaned = name
    for _ in range(3):
        cleaned = _NOISE.sub("", cleaned).strip()
    queries.append(cleaned)
    queries.append(" ".join(name.split()[:3]))
    result: List[str] = []
    for query in queries:
        if query and query.lower() not in (q.lower() for q in result):
            result.append(query)
    return result


def suggest_symbols(
    provider: MarketDataProvider,
    products: Dict[str, dict],
    catalogue: Optional[Dict[str, str]] = None,
) -> Dict[str, SymbolChoice]:
    """ISIN → proposed symbol. `products` is ParsedStatement.products;
    `catalogue` maps ISINs already known to their symbol."""
    catalogue = catalogue or {}
    choices: Dict[str, SymbolChoice] = {}
    to_price: Dict[str, List[Candidate]] = {}
    for isin, info in products.items():
        choice = choices[isin] = SymbolChoice(isin=isin, name=info["name"], currency=info["currency"])
        if isin in catalogue:
            choice.chosen = catalogue[isin]
            choice.from_catalogue = True
            continue
        seen: Dict[str, Candidate] = {}
        for index, query in enumerate(search_queries(isin, info)):
            if seen and index >= 2:         # the ISIN and one name query found listings
                break
            try:
                results = provider.search(query, limit=15)
            except MarketDataError as exc:
                logger.info("Búsqueda de %s fallida: %s", query, exc)
                continue
            for result in results:
                if result.symbol not in seen and listing_currency(result.symbol) == info["currency"]:
                    seen[result.symbol] = Candidate(result.symbol, result.exchange, result.name)
        choice.candidates = list(seen.values())
        to_price[isin] = choice.candidates

    trade_days = [d for info in products.values() for d, _ in info["trades"]]
    symbols = sorted({c.symbol for cands in to_price.values() for c in cands})
    closes: Dict[str, List[Tuple[date, float]]] = {}
    if symbols and trade_days:
        try:
            closes = provider.daily_closes(symbols, start=min(trade_days) - timedelta(days=7),
                                           end=max(trade_days))
        except MarketDataError as exc:
            logger.info("Precios de candidatos no disponibles: %s", exc)

    for isin, candidates in to_price.items():
        trades = products[isin]["trades"]
        for candidate in candidates:
            rows = closes.get(candidate.symbol, [])
            gaps = []
            for day, paid in trades:
                before = [close for d, close in rows if d <= day and (day - d).days <= 5]
                if before and paid:
                    gaps.append(before[-1] / paid - 1)
            candidate.gap = median(gaps) if gaps else None
        usable = [c for c in candidates if c.gap is not None and abs(c.gap) <= MAX_PRICE_GAP]
        best = min(usable, key=lambda c: abs(c.gap), default=None)
        # The same ticker on other exchanges within a point of the best is an
        # equally plausible listing (a close is not the execution price): take
        # the main venue. A different ticker (e.g. VUAA, the accumulating class
        # of VUSA) is a different product and never wins on venue alone.
        # A regional German quote (IDA.DU) loses to the home market (IDR.MC)
        # when both price within half a point: same share, local ticker.
        ranked = [] if best is None else sorted(
            (c for c in usable
             if (_root(c.symbol) == _root(best.symbol) and abs(c.gap) <= abs(best.gap) + 0.01)
             or (_rank(best) >= 3 and _rank(c) <= 1 and abs(c.gap) <= abs(best.gap) + 0.005)),
            key=lambda c: (_rank(c), abs(c.gap)))
        choice = choices[isin]
        choice.candidates.sort(key=lambda c: (c.gap is None, abs(c.gap or 0)))
        if ranked:
            choice.chosen = ranked[0].symbol
        elif not trades and candidates:
            choice.chosen = candidates[0].symbol    # only dividends: no price to compare
    return choices
