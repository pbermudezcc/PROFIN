"""
app/investments/market_data.py
==============================
FAMILYFIN — Market-data providers (quotes, FX, symbol search).

Everything outside this module talks to the MarketDataProvider protocol,
so Yahoo can be swapped (Stooq, Twelve Data...) without touching the cache,
the portfolio engine or the GUI. Tests use a fake provider and never hit
the network.

YahooProvider notes (yfinance 1.7, verified in the F0 spike):
  * yfinance is imported lazily: it pulls in pandas/curl_cffi and is only
    needed when a refresh or search actually runs.
  * `Ticker.info` costs ~1.5 s per symbol, so security_info() is called
    once when a security is created; prices come from one batched
    `yf.download` call (~1 s for a dozen symbols over a year).
  * Daily "Close" is split-adjusted but not dividend-adjusted; during market
    hours today's bar is the live (~15 min delayed) price.
  * FX pairs follow Yahoo's EURUSD=X convention: units of currency per EUR.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from app.db.models import AssetType

logger = logging.getLogger(__name__)

DailyCloses = Dict[str, List[Tuple[date, float]]]

# Yahoo quoteType → our asset type. Other types (crypto, indices, options)
# are not offered by search.
_QUOTE_TYPES = {
    "EQUITY":     AssetType.stock,
    "ETF":        AssetType.etf,
    "MUTUALFUND": AssetType.fund,
}

# Quote currencies Yahoo reports in minor units: (ISO currency, divisor).
_MINOR_UNITS = {
    "GBp": ("GBP", 100.0),
    "GBX": ("GBP", 100.0),
    "ZAc": ("ZAR", 100.0),
    "ILA": ("ILS", 100.0),
}


class MarketDataError(RuntimeError):
    """The provider could not answer. Message in Spanish, shown to the user."""


@dataclass(frozen=True)
class SearchResult:
    symbol:     str
    name:       str
    asset_type: AssetType
    exchange:   str


@dataclass(frozen=True)
class SecurityInfo:
    symbol:     str
    name:       str
    asset_type: AssetType
    currency:   str
    exchange:   Optional[str] = None
    sector:     Optional[str] = None
    country:    Optional[str] = None


@dataclass(frozen=True)
class CorporateActions:
    """Per-share cash dividends (quote currency) and split ratios, by ex-date."""
    dividends: Tuple[Tuple[date, float], ...] = ()
    splits:    Tuple[Tuple[date, float], ...] = ()


def fx_pair(currency: str) -> Optional[Tuple[str, float]]:
    """(ISO currency whose EUR rate is needed, divisor for minor units).

    None for EUR. A price in GBp is converted with the GBP rate × 100.
    """
    if currency == "EUR":
        return None
    return _MINOR_UNITS.get(currency, (currency, 1.0))


def fx_symbol(iso_currency: str) -> str:
    return f"EUR{iso_currency}=X"


class MarketDataProvider(Protocol):
    name: str

    def search(self, text: str, limit: int = 10) -> List[SearchResult]: ...

    def security_info(self, symbol: str) -> SecurityInfo: ...

    def daily_closes(
        self, symbols: Sequence[str], start: date, end: Optional[date] = None
    ) -> DailyCloses: ...

    def corporate_actions(self, symbol: str) -> CorporateActions: ...


# ---------------------------------------------------------------------------
# Yahoo Finance (yfinance)
# ---------------------------------------------------------------------------
class YahooProvider:
    name = "Yahoo Finance"

    _CONNECTION_ERROR = (
        "No se pudo conectar con Yahoo Finance. Comprueba la conexión a "
        "internet; se muestran los últimos precios guardados."
    )

    @staticmethod
    def _yf():
        import yfinance
        return yfinance

    def search(self, text: str, limit: int = 10) -> List[SearchResult]:
        text = (text or "").strip()
        if not text:
            return []
        try:
            quotes = self._yf().Search(
                text, max_results=limit, news_count=0, raise_errors=True
            ).quotes
        except Exception as exc:
            logger.warning("Búsqueda en Yahoo fallida (%r): %s", text, exc)
            raise MarketDataError(self._CONNECTION_ERROR) from exc
        results = []
        for quote in quotes:
            asset_type = _QUOTE_TYPES.get(quote.get("quoteType", ""))
            symbol = quote.get("symbol")
            if asset_type is None or not symbol:
                continue
            results.append(SearchResult(
                symbol=symbol,
                name=quote.get("longname") or quote.get("shortname") or symbol,
                asset_type=asset_type,
                exchange=quote.get("exchDisp") or quote.get("exchange") or "",
            ))
        return results

    def security_info(self, symbol: str) -> SecurityInfo:
        try:
            info = self._yf().Ticker(symbol).info or {}
        except Exception as exc:
            logger.warning("Datos de %s no disponibles: %s", symbol, exc)
            raise MarketDataError(self._CONNECTION_ERROR) from exc
        currency = info.get("currency")
        if not currency:
            raise MarketDataError(f"Yahoo Finance no tiene datos del valor {symbol}.")
        return SecurityInfo(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName") or symbol,
            asset_type=_QUOTE_TYPES.get(info.get("quoteType", ""), AssetType.stock),
            currency=currency,
            exchange=info.get("fullExchangeName") or info.get("exchange"),
            sector=info.get("sector"),
            country=info.get("country"),
        )

    def daily_closes(
        self, symbols: Sequence[str], start: date, end: Optional[date] = None
    ) -> DailyCloses:
        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        try:
            frame = self._yf().download(
                symbols,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat() if end else None,
                interval="1d",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=True,
                multi_level_index=True,
            )
        except Exception as exc:
            logger.warning("Descarga de cotizaciones fallida: %s", exc)
            raise MarketDataError(self._CONNECTION_ERROR) from exc
        if frame is None or frame.empty or "Close" not in frame.columns.get_level_values(0):
            raise MarketDataError(self._CONNECTION_ERROR)

        closes = frame["Close"]
        result: DailyCloses = {}
        for symbol in symbols:
            if symbol not in closes.columns:
                continue
            series = closes[symbol].dropna()
            rows = [(index.date(), float(value)) for index, value in series.items()]
            if rows:
                result[symbol] = rows
        return result

    def corporate_actions(self, symbol: str) -> CorporateActions:
        """Dividends and splits Yahoo knows for a symbol (whole history).

        Yahoo's `actions` frame has one row per event day with both columns;
        a 0 in "Stock Splits" (or "Dividends") means no such event that day.
        """
        try:
            frame = self._yf().Ticker(symbol).actions
        except Exception as exc:
            logger.warning("Dividendos/splits de %s no disponibles: %s", symbol, exc)
            raise MarketDataError(self._CONNECTION_ERROR) from exc
        if frame is None or frame.empty:
            return CorporateActions()
        dividends, splits = [], []
        for index, row in frame.iterrows():
            day = index.date()
            if float(row.get("Dividends", 0) or 0) > 0:
                dividends.append((day, float(row["Dividends"])))
            if float(row.get("Stock Splits", 0) or 0) > 0:
                splits.append((day, float(row["Stock Splits"])))
        return CorporateActions(tuple(dividends), tuple(splits))
