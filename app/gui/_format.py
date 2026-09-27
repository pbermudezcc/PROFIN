"""
app/gui/_format.py
==================
FAMILYFIN — Spanish number formatting for the investment screens.

1234.5 → "1.234,50"; thousands with ".", decimals with ",".
"""
from __future__ import annotations

from datetime import date
from typing import Optional

_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "GBp": "p", "CHF": "CHF"}


def fmt_number(value: float, decimals: int = 2) -> str:
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_quantity(value: Optional[float]) -> str:
    """Shares: no trailing zeros, up to 6 decimals (fund units)."""
    if value is None:
        return "—"
    text = fmt_number(value, 6).rstrip("0").rstrip(",")
    return text


def fmt_eur(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ("−" if value < 0 else "")
    return f"{sign}{fmt_number(abs(value))} €"


def fmt_money(value: Optional[float], currency: Optional[str], decimals: int = 2) -> str:
    if value is None:
        return "—"
    symbol = _SYMBOLS.get(currency or "EUR", currency or "")
    return f"{fmt_number(value, decimals)} {symbol}"


def fmt_pct(value: Optional[float], signed: bool = True) -> str:
    """0.1234 → "+12,34 %"."""
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ("−" if value < 0 else "")
    return f"{sign}{fmt_number(abs(value) * 100)} %"


def fmt_date(value: Optional[date]) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"
