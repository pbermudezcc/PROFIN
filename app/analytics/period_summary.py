"""
app/analytics/period_summary.py
================================
FAMILYFIN — Period aggregates for the Gastos / Ingresos views.

All sums, counts and groupings run in SQLite (one small result set per
chart) instead of loading every Transaction of the period as ORM objects
and adding them up in Python. Amounts are returned as positive magnitudes
(abs), rounded to cents.

Category labels: transactions are grouped by category name, so two
categories with the same name under different types (e.g. "Hipoteca")
share one slice, as the views always showed them. Uncategorized rows, or
rows whose category no longer exists, are labelled UNCATEGORIZED_LABEL.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Category, Transaction, TransactionType

UNCATEGORIZED_LABEL = "Sin categorizar"


@dataclass
class DetailRow:
    """One row of the detail table (latest transactions first)."""
    transaction_date: date
    description:      str
    category:         Optional[str]
    amount:           float


@dataclass
class PeriodSummary:
    total:       float = 0.0
    count:       int = 0
    max_amount:  float = 0.0
    categorized: int = 0
    # [(category label, total)] sorted by total desc
    by_category: List[Tuple[str, float]] = field(default_factory=list)
    # [(first day of month, [(category label, total)] top N desc)] by month asc
    by_month_category: List[Tuple[date, List[Tuple[str, float]]]] = field(
        default_factory=list
    )
    # [(day, total)] by day asc
    by_day: List[Tuple[date, float]] = field(default_factory=list)
    latest: List[DetailRow] = field(default_factory=list)

    @property
    def average(self) -> float:
        return round(self.total / self.count, 2) if self.count else 0.0

    @property
    def categorized_pct(self) -> float:
        return self.categorized / self.count * 100 if self.count else 0.0


def _period_filter(user_id: int, txn_type: TransactionType, d_from: date, d_to: date):
    return (
        Transaction.user_id == user_id,
        Transaction.type == txn_type,
        Transaction.transaction_date >= d_from,
        Transaction.transaction_date <= d_to,
    )


def period_totals(
    session:  Session,
    user_id:  int,
    txn_type: TransactionType,
    d_from:   date,
    d_to:     date,
) -> Tuple[float, int]:
    """Return (sum of |amount|, row count) for the period."""
    total, count = (
        session.query(
            func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0),
            func.count(Transaction.id),
        )
        .filter(*_period_filter(user_id, txn_type, d_from, d_to))
        .one()
    )
    return round(float(total), 2), int(count)


def category_totals(
    session:  Session,
    user_id:  int,
    txn_type: TransactionType,
    d_from:   date,
    d_to:     date,
) -> List[Tuple[str, float]]:
    """[(category label, sum of |amount|)] for the period, sorted desc."""
    names = _category_names(session, user_id)
    rows = (
        session.query(
            Transaction.category_id,
            func.sum(func.abs(Transaction.amount)),
        )
        .filter(*_period_filter(user_id, txn_type, d_from, d_to))
        .group_by(Transaction.category_id)
        .all()
    )
    return _merge_by_label(rows, names)


def summarize_period(
    session:       Session,
    user_id:       int,
    txn_type:      TransactionType,
    d_from:        date,
    d_to:          date,
    top_per_month: int = 4,
    latest_limit:  int = 50,
) -> PeriodSummary:
    """Compute every figure the Gastos/Ingresos view shows for one period."""
    where = _period_filter(user_id, txn_type, d_from, d_to)
    names = _category_names(session, user_id)
    summary = PeriodSummary()

    total, count, max_amount, categorized = (
        session.query(
            func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0),
            func.count(Transaction.id),
            func.coalesce(func.max(func.abs(Transaction.amount)), 0.0),
            func.count(Transaction.category_id),
        )
        .filter(*where)
        .one()
    )
    summary.total = round(float(total), 2)
    summary.count = int(count)
    summary.max_amount = round(float(max_amount), 2)
    summary.categorized = int(categorized)
    if not summary.count:
        return summary

    summary.by_category = category_totals(session, user_id, txn_type, d_from, d_to)

    month = func.strftime("%Y-%m", Transaction.transaction_date)
    month_rows = (
        session.query(
            month,
            Transaction.category_id,
            func.sum(func.abs(Transaction.amount)),
        )
        .filter(*where)
        .group_by(month, Transaction.category_id)
        .all()
    )
    rows_by_month: Dict[str, list] = defaultdict(list)
    for month_key, category_id, amount in month_rows:
        rows_by_month[month_key].append((category_id, amount))
    for month_key in sorted(rows_by_month):
        year, month_number = (int(part) for part in month_key.split("-"))
        top = _merge_by_label(rows_by_month[month_key], names)[:top_per_month]
        summary.by_month_category.append((date(year, month_number, 1), top))

    day_rows = (
        session.query(
            Transaction.transaction_date,
            func.sum(func.abs(Transaction.amount)),
        )
        .filter(*where)
        .group_by(Transaction.transaction_date)
        .order_by(Transaction.transaction_date)
        .all()
    )
    summary.by_day = [(day, round(float(amount), 2)) for day, amount in day_rows]

    latest_rows = (
        session.query(
            Transaction.transaction_date,
            Transaction.issuer_name,
            Transaction.concept,
            Transaction.category_id,
            Transaction.amount,
        )
        .filter(*where)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .limit(latest_limit)
        .all()
    )
    summary.latest = [
        DetailRow(
            transaction_date=txn_date,
            description=issuer or concept or "—",
            category=names.get(category_id),
            amount=round(abs(amount), 2),
        )
        for txn_date, issuer, concept, category_id, amount in latest_rows
    ]
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _category_names(session: Session, user_id: int) -> Dict[int, str]:
    return dict(
        session.query(Category.id, Category.name)
        .filter(Category.user_id == user_id)
        .all()
    )


def _merge_by_label(
    rows:  List[Tuple[Optional[int], float]],
    names: Dict[int, str],
) -> List[Tuple[str, float]]:
    totals: Dict[str, float] = defaultdict(float)
    for category_id, amount in rows:
        totals[names.get(category_id, UNCATEGORIZED_LABEL)] += float(amount or 0.0)
    return sorted(
        ((label, round(value, 2)) for label, value in totals.items()),
        key=lambda item: item[1],
        reverse=True,
    )
