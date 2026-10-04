"""Resumen: the web home screen."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.analytics.dashboard_summary import DashboardSummary, dashboard_summary, range_bounds
from app.investments.snapshot import portfolio_snapshot
from app.web.deps import DB, USER, WebUser
from app.web.periods import MONTHS
from app.web.render import render

logger = logging.getLogger(__name__)
router = APIRouter()

RANGES = [("month", "Mes"), ("year", "Año"), ("all", "Todo")]


def _summary(db: Session, user: WebUser, rango: str) -> DashboardSummary:
    rango = rango if rango in dict(RANGES) else "month"
    date_from, date_to = range_bounds(rango)
    return dashboard_summary(db, user.id, date_from, date_to, recent_limit=8)


def _charts(summary: DashboardSummary) -> Dict[str, Any]:
    last = summary.monthly[-12:]
    return {
        "categories": {
            "type": "doughnut",
            "labels": [label for label, _ in summary.by_category],
            "values": [round(v, 2) for _, v in summary.by_category],
        },
        "monthly": {
            "type": "bar",
            "labels": [f"{MONTHS[m.month][:3]} {str(m.year)[2:]}" for m in last],
            "series": [
                {"name": "Ingresos", "color": "income", "values": [m.income for m in last]},
                {"name": "Gastos", "color": "expense", "values": [m.expense for m in last]},
            ],
        },
    }


def _portfolio(db: Session, user: WebUser) -> Optional[Any]:
    try:
        snapshot = portfolio_snapshot(db, user.id)
    except Exception:
        logger.exception("Web: resumen de cartera")
        return None
    return snapshot.totals if snapshot.rows else None


@router.get("/")
def home(request: Request, rango: str = "month", user: WebUser = USER, db: Session = DB) -> Response:
    summary = _summary(db, user, rango)
    return render(request, "resumen.html", {
        "s": summary,
        "rango": rango if rango in dict(RANGES) else "month",
        "ranges": RANGES,
        "charts": _charts(summary),
        "portfolio": _portfolio(db, user),
    }, user=user, active="resumen")


@router.get("/api/resumen")
def api_home(rango: str = "month", user: WebUser = USER, db: Session = DB) -> JSONResponse:
    s = _summary(db, user, rango)
    return JSONResponse({
        "date_from": s.date_from.isoformat(),
        "date_to": s.date_to.isoformat(),
        "balance": round(s.balance, 2),
        "income": round(s.income, 2),
        "expense": round(s.expense, 2),
        "investment": round(s.investment, 2),
        "savings_rate": round(s.savings_rate, 2),
        "uncategorized": s.uncategorized,
        "charts": _charts(s),
        "recent": [
            {"id": r.id, "date": r.transaction_date.isoformat(), "description": r.issuer_name or r.concept,
             "amount": r.amount, "category": r.category_name}
            for r in s.recent
        ],
    })
