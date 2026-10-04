"""
app/analytics/dashboard_summary.py
==================================
FAMILYFIN — Aggregates behind the Dashboard (desktop) and Resumen (web).

Every figure is scoped to one user and computed in SQLite. Expenses and
investments are returned as positive magnitudes. Each section is computed
independently: a failure in one (logged) leaves its defaults and never
blanks the whole summary.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.db.models import Category, Transaction, TransactionType

logger = logging.getLogger(__name__)

UNCATEGORIZED_LABEL = "Sin categorizar"


@dataclass
class RecentRow:
    id:               int
    transaction_date: date
    issuer_name:      Optional[str]
    concept:          Optional[str]
    amount:           float
    category_name:    Optional[str]


@dataclass
class MonthFlow:
    year:    int
    month:   int
    income:  float
    expense: float


@dataclass
class DashboardSummary:
    date_from:     date
    date_to:       date
    balance:       float = 0.0
    income:        float = 0.0
    expense:       float = 0.0
    investment:    float = 0.0
    uncategorized: int = 0
    # [(category label, total)] sorted by total desc; uncategorized
    # expenses appear as "Sin categorizar (n)" when there are any.
    by_category: List[Tuple[str, float]] = field(default_factory=list)
    # every month with movements, ascending
    monthly: List[MonthFlow] = field(default_factory=list)
    recent:  List[RecentRow] = field(default_factory=list)

    @property
    def savings_rate(self) -> float:
        if self.income <= 0:
            return 0.0
        return (self.income - self.expense) / self.income * 100


def range_bounds(range_key: str, today: Optional[date] = None) -> Tuple[date, date]:
    """Period of the dashboard selector: 'month', 'year' or anything else = all."""
    today = today or date.today()
    if range_key == "month":
        return date(today.year, today.month, 1), today
    if range_key == "year":
        return date(today.year, 1, 1), today
    return date(2000, 1, 1), today


def dashboard_summary(
    session: Session,
    user_id: int,
    date_from: date,
    date_to: date,
    recent_limit: int = 5,
) -> DashboardSummary:
    summary = DashboardSummary(date_from=date_from, date_to=date_to)

    try:
        # Saldo of the latest transaction that has one (running_balance is
        # nullable; a row without it must not crash the summary).
        summary.balance = (
            session.query(Transaction.running_balance)
            .filter(Transaction.user_id == user_id,
                    Transaction.running_balance.isnot(None))
            .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
            .limit(1)
            .scalar()
        ) or 0.0

        totals = {
            row.type: (row.total or 0.0)
            for row in (
                session.query(Transaction.type, func.sum(Transaction.amount).label("total"))
                .filter(
                    Transaction.user_id == user_id,
                    Transaction.transaction_date >= date_from,
                    Transaction.transaction_date <= date_to,
                )
                .group_by(Transaction.type)
                .all()
            )
        }
        summary.income = totals.get(TransactionType.income, 0.0)
        summary.expense = abs(totals.get(TransactionType.expense, 0.0))
        summary.investment = abs(totals.get(TransactionType.investment, 0.0))

        summary.uncategorized = (
            session.query(func.count(Transaction.id))
            .filter(
                Transaction.user_id == user_id,
                Transaction.category_id.is_(None),
                Transaction.transaction_date >= date_from,
                Transaction.transaction_date <= date_to,
            )
            .scalar() or 0
        )
    except Exception:
        logger.exception("Resumen: totales del periodo")
        summary.balance = summary.income = summary.expense = summary.investment = 0.0
        summary.uncategorized = 0

    try:
        period_expense = (
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.expense,
            Transaction.transaction_date >= date_from,
            Transaction.transaction_date <= date_to,
        )
        by_category = [
            (name, abs(total or 0.0))
            for name, total in (
                session.query(Category.name, func.sum(Transaction.amount))
                .join(Transaction, Transaction.category_id == Category.id)
                .filter(*period_expense)
                .group_by(Category.name)
                .all()
            )
            if total
        ]
        uncat_total, uncat_n = (
            session.query(func.sum(Transaction.amount), func.count(Transaction.id))
            .filter(*period_expense, Transaction.category_id.is_(None))
            .one()
        )
        if uncat_total:
            by_category.append((f"{UNCATEGORIZED_LABEL} ({int(uncat_n)})", abs(uncat_total)))
        by_category.sort(key=lambda item: item[1], reverse=True)
        summary.by_category = by_category
    except Exception:
        logger.exception("Resumen: gastos por categoría")

    try:
        monthly: Dict[Tuple[int, int], Dict[str, float]] = defaultdict(
            lambda: {"income": 0.0, "expense": 0.0}
        )
        for yr, mo, txn_type, total in (
            session.query(
                extract("year", Transaction.transaction_date).label("yr"),
                extract("month", Transaction.transaction_date).label("mo"),
                Transaction.type,
                func.sum(Transaction.amount),
            )
            .filter(Transaction.user_id == user_id)
            .group_by("yr", "mo", Transaction.type)
            .all()
        ):
            key = (int(yr), int(mo))
            if txn_type == TransactionType.income:
                monthly[key]["income"] += abs(total or 0.0)
            elif txn_type == TransactionType.expense:
                monthly[key]["expense"] += abs(total or 0.0)
        summary.monthly = [
            MonthFlow(yr, mo, round(v["income"], 2), round(v["expense"], 2))
            for (yr, mo), v in sorted(monthly.items())
        ]
    except Exception:
        logger.exception("Resumen: evolución mensual")

    try:
        names = dict(
            session.query(Category.id, Category.name)
            .filter(Category.user_id == user_id)
            .all()
        )
        summary.recent = [
            RecentRow(
                id=txn.id,
                transaction_date=txn.transaction_date,
                issuer_name=txn.issuer_name,
                concept=txn.concept,
                amount=txn.amount,
                category_name=names.get(txn.category_id),
            )
            for txn in (
                session.query(Transaction)
                .filter(Transaction.user_id == user_id)
                .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
                .limit(recent_limit)
                .all()
            )
        ]
    except Exception:
        logger.exception("Resumen: últimas transacciones")

    return summary
