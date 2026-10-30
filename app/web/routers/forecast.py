"""Previsión: fixed-spending baseline, this month's bills, next month's
forecast and the aportes between members. Mío (the signed-in member) or
Familia (every member but the demo account, aggregates only)."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.analytics.forecast import Forecast, family_forecast, month_label, user_forecast
from app.categorization.admin_service import NATURE_LABELS, set_natures, suggested_natures
from app.db.models import AuditAction, Category, CategoryLevel
from app.web.deps import DB, USER, WebUser, is_family_member, notify_change, require_family_member
from app.web.render import audit, hx_trigger, parse_int, render

router = APIRouter()

VIEWS = [("mio", "Mío"), ("familia", "Familia")]


def _forecast(db: Session, user: WebUser, vista: str) -> Forecast:
    if vista == "familia":
        require_family_member(db, user)
        return family_forecast(db)
    return user_forecast(db, user.id)


def _charts(fc: Forecast) -> Dict[str, Any]:
    charts: Dict[str, Any] = {
        "split": {
            "type": "stacked",
            "labels": [s.label for s in fc.monthly_split],
            "series": [
                {"name": "Fijos", "color": "accent", "values": [s.fixed for s in fc.monthly_split]},
                {"name": "Variables", "color": "expense", "values": [s.variable for s in fc.monthly_split]},
            ],
        },
    }
    if fc.contributions:
        months = fc.window
        charts["aportes"] = {
            "type": "bar",
            "labels": [month_label(m) for m in months],
            "series": [{"name": f"{c.sender} → {c.receiver}", "values": [c.by_month.get(m, 0.0) for m in months]}
                       for c in fc.contributions],
        }
    return charts


def _has_marks(db: Session, user_id: int) -> bool:
    return db.query(Category.id).filter(Category.user_id == user_id, Category.nature.isnot(None)).first() is not None


@router.get("/prevision")
def forecast_page(request: Request, vista: str = "mio", user: WebUser = USER, db: Session = DB) -> Response:
    vista = vista if vista in dict(VIEWS) else "mio"
    fc = _forecast(db, user, vista)
    suggestions = []
    if vista == "mio" and not _has_marks(db, user.id):
        found = suggested_natures(db, user.id)
        names = dict(db.query(Category.id, Category.name).filter(Category.id.in_(found)))
        suggestions = sorted(((cid, names[cid], nature) for cid, nature in found.items()),
                             key=lambda s: (s[2], s[1].lower()))
    has_categories = db.query(Category.id).filter(
        Category.user_id == user.id, Category.level == CategoryLevel.category).first() is not None
    return render(request, "prevision.html", {
        "fc": fc,
        "vista": vista,
        "views": VIEWS if is_family_member(db, user) else [],
        "charts": _charts(fc),
        "suggestions": suggestions,
        "has_categories": has_categories,
        "nature_labels": NATURE_LABELS,
    }, user=user, active="prevision")


@router.post("/prevision/marcar")
async def mark_suggested(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    """The empty-state checklist: each ticked category gets the nature it was
    suggested with (fijo / traspaso)."""
    form = await request.form()
    natures: Dict[int, str] = {}
    for key, value in form.multi_items():
        cid = parse_int(key[4:]) if key.startswith("cat_") else None
        if cid is not None and value in ("fijo", "traspaso"):
            natures[cid] = value
    if not natures:
        response = Response(status_code=204)
        hx_trigger(response, toastError="Marca al menos una categoría.")
        return response
    try:
        changed = set_natures(db, user.id, natures)
    except LookupError:
        raise HTTPException(status_code=404)
    audit(db, request, user, "categories", None, AuditAction.update,
          new=f"naturaleza desde Previsión: {', '.join(f'{k}={v}' for k, v in sorted(natures.items()))}")
    notify_change(request, user, "transactions")
    response = Response(status_code=204)
    hx_trigger(response, toast=f"{changed} categorías marcadas", refreshPage=True)
    return response


@router.get("/api/prevision")
def forecast_api(vista: str = "mio", user: WebUser = USER, db: Session = DB) -> JSONResponse:
    fc = _forecast(db, user, vista if vista in dict(VIEWS) else "mio")
    return JSONResponse({
        "months": fc.months,
        "provisional": fc.provisional,
        "fixed_baseline": fc.fixed_baseline,
        "variable_median": fc.variable_median,
        "next_month": {
            "label": fc.next_month_label,
            "fixed": fc.next_fixed,
            "other_fixed": fc.next_other_fixed,
            "expenses": fc.next_expenses,
            "income": fc.next_income,
            "saving": fc.next_saving,
            "items": [{"label": i.label, "category": i.category, "kind": i.kind, "expected": i.expected,
                       "day": i.day} for i in fc.next_month],
        },
        "this_month": [{"label": i.label, "category": i.category, "kind": i.kind, "status": i.status,
                        "expected": i.expected, "actual": i.actual, "day": i.day, "above_pct": i.above_pct}
                       for i in fc.this_month],
        "contributions": [{"sender": c.sender, "receiver": c.receiver, "average": c.average, "total": c.total,
                           "share_pct": c.share_pct, "this_month": c.this_month} for c in fc.contributions],
    })
