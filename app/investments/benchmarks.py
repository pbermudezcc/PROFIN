"""
app/investments/benchmarks.py
=============================
FAMILYFIN — Indices the portfolio can be compared against.

EUR listings only, so the comparison needs no FX. They are catalogued as
ordinary securities (performance.ensure_benchmark) and prices.py refreshes
them together with the holdings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class Benchmark:
    key:      str
    symbol:   str
    name:     str
    exchange: str


# EUR listings only: the comparison needs no FX conversion.
BENCHMARKS: Dict[str, Benchmark] = {
    "msci_world": Benchmark("msci_world", "IWDA.AS", "MSCI World (iShares IWDA)", "Amsterdam"),
    "sp500":      Benchmark("sp500", "SXR8.DE", "S&P 500 (iShares SXR8)", "XETRA"),
    "ibex35":     Benchmark("ibex35", "^IBEX", "IBEX 35", "BME"),
}
BENCHMARK_SYMBOLS = tuple(b.symbol for b in BENCHMARKS.values())
DEFAULT_BENCHMARK = "msci_world"
