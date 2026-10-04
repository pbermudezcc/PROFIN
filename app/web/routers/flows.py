"""Gastos / Ingresos: totals, charts and drill-down per period."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.analytics.period_summary import PeriodSummary, summarize_period
from app.db.models import TransactionType
from app.web.deps import DB, USER, WebUser
from app.web.periods import MONTHS, PERIODS, Period, resolve_period
from app.web.render import parse_date, render

router = APIRouter()

KINDS = {
    "gastos": (TransactionType.expense, "Gastos", "expense"),
    "ingresos": (TransactionType.income, "Ingresos", "income"),
}


def _period(periodo: Optional[str], ref: Optional[str], desde: Optional[str], hasta: Optional[str]) -> Period:
    return resolve_period(periodo, parse_date(ref), parse_date(desde), parse_date(hasta))


def _charts(summary: PeriodSummary, color: str) -> Dict[str, Any]:
    labels = sorted({label for _, top in summary.by_month_category for label, _ in top},
                    key=lambda label: -sum(v for _, top in summary.by_month_category
                                           for lab, v in top if lab == label))
    return {
        "categories": {
            "type": "doughnut",
            "labels": [label for label, _ in summary.by_category],
            "values": [round(v, 2) for _, v in summary.by_category],
        },
        "monthly": {
            "type": "stacked",
            "labels": [f"{MONTHS[m.month][:3]} {str(m.year)[2:]}" for m, _ in summary.by_month_category],
            "series": [
                {"name": label,
                 "values": [round(dict(top).get(label, 0.0), 2) for _, top in summary.by_month_category]}
                for label in labels
            ],
        },
        "daily": {
            "type": "line",
            "color": color,
            "labels": [d.strftime("%d/%m") for d, _ in summary.by_day],
            "values": [v for _, v in summary.by_day],
        },
    }


def _summary(kind: str, db: Session, user: WebUser, period: Period):
    if kind not in KINDS:
        raise HTTPException(status_code=404)
    txn_type, title, color = KINDS[kind]
    summary = summarize_period(db, user.id, txn_type, period.date_from, period.date_to, latest_limit=15)
    return summary, txn_type, title, color


def flows_page(
    request: Request, kind: str,
    periodo: Optional[str] = None, ref: Optional[str] = None,
    desde: Optional[str] = None, hasta: Optional[str] = None,
    user: WebUser = USER, db: Session = DB,
) -> Response:
    period = _period(periodo, ref, desde, hasta)
    summary, txn_type, title, color = _summary(kind, db, user, period)
    total = summary.total or 1.0
    return render(request, "flujos.html", {
        "kind": kind,
        "txn_type": txn_type.value,
        "title": title,
        "color": color,
        "period": period,
        "periods": PERIODS,
        "s": summary,
        "shares": [(label, value, value / total) for label, value in summary.by_category],
        "charts": _charts(summary, color),
    }, user=user, active="flujos")


@router.get("/api/flujos/{kind}")
def flows_api(
    kind: str,
    periodo: Optional[str] = None, ref: Optional[str] = None,
    desde: Optional[str] = None, hasta: Optional[str] = None,
    user: WebUser = USER, db: Session = DB,
) -> JSONResponse:
    period = _period(periodo, ref, desde, hasta)
    summary, _, _, color = _summary(kind, db, user, period)
    return JSONResponse({
        "date_from": period.date_from.isoformat(),
        "date_to": period.date_to.isoformat(),
        "total": summary.total,
        "count": summary.count,
        "average": summary.average,
        "max": summary.max_amount,
        "categorized_pct": round(summary.categorized_pct, 1),
        "charts": _charts(summary, color),
    })


@router.get("/gastos")
def expenses_page(
    request: Request,
    periodo: Optional[str] = None, ref: Optional[str] = None,
    desde: Optional[str] = None, hasta: Optional[str] = None,
    user: WebUser = USER, db: Session = DB,
) -> Response:
    return flows_page(request, "gastos", periodo, ref, desde, hasta, user, db)


@router.get("/ingresos")
def incomes_page(
    request: Request,
    periodo: Optional[str] = None, ref: Optional[str] = None,
    desde: Optional[str] = None, hasta: Optional[str] = None,
    user: WebUser = USER, db: Session = DB,
) -> Response:
    return flows_page(request, "ingresos", periodo, ref, desde, hasta, user, db)
