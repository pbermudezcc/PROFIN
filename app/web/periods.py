"""
app/web/periods.py
==================
FAMILYFIN — Period selector (Mes / Trimestre / Año / 12 meses / Todo /
Personalizado) with previous/next navigation for the web screens.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

MONTHS = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
          "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

PERIODS = [("mes", "Mes"), ("trimestre", "Trimestre"), ("anio", "Año"),
           ("12m", "12 meses"), ("todo", "Todo")]


@dataclass
class Period:
    key:       str
    date_from: date
    date_to:   date
    label:     str
    prev_ref:  Optional[date] = None
    next_ref:  Optional[date] = None


def _month_end(day: date) -> date:
    first_next = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def resolve_period(
    key: Optional[str],
    ref: Optional[date] = None,
    custom_from: Optional[date] = None,
    custom_to: Optional[date] = None,
    today: Optional[date] = None,
) -> Period:
    today = today or date.today()
    ref = ref or today
    key = key or "mes"

    if key == "custom" and custom_from and custom_to:
        start, end = sorted((custom_from, custom_to))
        return Period("custom", start, end, f"{start:%d/%m/%Y} – {end:%d/%m/%Y}")
    if key == "trimestre":
        q_month = (ref.month - 1) // 3 * 3 + 1
        start = date(ref.year, q_month, 1)
        end = _month_end(_add_months(start, 2))
        nxt = _add_months(start, 3)
        return Period(key, start, end, f"T{(q_month - 1) // 3 + 1} {ref.year}",
                      _add_months(start, -3), nxt if nxt <= today else None)
    if key == "anio":
        start, end = date(ref.year, 1, 1), date(ref.year, 12, 31)
        return Period(key, start, end, str(ref.year), date(ref.year - 1, 1, 1),
                      date(ref.year + 1, 1, 1) if ref.year < today.year else None)
    if key == "12m":
        start = _add_months(date(today.year, today.month, 1), -11)
        return Period(key, start, today, "Últimos 12 meses")
    if key == "todo":
        return Period(key, date(2000, 1, 1), today, "Todo el historial")

    start = date(ref.year, ref.month, 1)
    nxt = _add_months(start, 1)
    return Period("mes", start, _month_end(start), f"{MONTHS[start.month]} {start.year}",
                  _add_months(start, -1), nxt if nxt <= today else None)
