"""
app/investments/importers/base.py
=================================
FAMILYFIN — What every broker statement reader produces.

A Statement is a list of ImportRow (one future ledger operation each) plus
the products they refer to. Readers differ a lot in what they know:

  * DEGIRO gives ISINs, exact quantities and prices;
  * MyInvestor's account CSV gives only a truncated name and an amount, so
    quantities are estimated from the day's price (`quantity_estimated`),
    and the kind of a positive amount (sale or dividend) is a guess the user
    can change in the preview (`kind_editable`).

Products are keyed by ISIN when the broker gives one, otherwise by a
normalised name ("NAME:ISHARESSP500ITSECTOR"); several spellings of one
security end up on the same symbol in the preview.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

KIND_BUY, KIND_SELL, KIND_DIVIDEND, KIND_IGNORE = "buy", "sell", "dividend", "ignore"


def is_isin(key: str) -> bool:
    return bool(ISIN_RE.match(key or ""))


@dataclass
class ImportRow:
    ref:          str                       # stable id → InvestmentTransaction.broker_ref
    day:          date
    key:          str                       # product key (ISIN or NAME:…)
    name:         str
    kind:         str                       # buy | sell | dividend | ignore
    amount_eur:   Optional[float]           # buy: total paid; sell/dividend: net received
    quantity:     Optional[float] = None
    price:        Optional[float] = None    # per share, in `currency`
    currency:     str = "EUR"
    fees_eur:     float = 0.0
    withheld_eur: float = 0.0
    fills:        int = 1
    note:         str = ""                  # shown in the preview / stored in notes
    gross_text:   str = ""                  # e.g. "5,42 $ brutos" for dividends
    foreign_gross:    Optional[float] = None    # dividend in `currency`, to estimate EUR
    foreign_withheld: float = 0.0
    quantity_estimated: bool = False
    kind_editable:      bool = False


@dataclass
class Statement:
    broker:    str
    rows:      List[ImportRow] = field(default_factory=list)
    products:  Dict[str, dict] = field(default_factory=dict)   # key → name, currency, product, trades, fund
    ignored:   Dict[str, int] = field(default_factory=dict)
    warnings:  List[str] = field(default_factory=list)
    first_day: Optional[date] = None
    last_day:  Optional[date] = None

    @property
    def needs_prices(self) -> bool:
        """True when quantities must be estimated once symbols are chosen."""
        return any(r.quantity_estimated for r in self.rows)
