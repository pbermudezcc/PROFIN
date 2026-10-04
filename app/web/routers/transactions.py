"""Movimientos: filtered list with infinite scroll, detail sheet, recategorize."""
from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.analytics.transaction_search import (
    UNCATEGORIZED,
    TransactionFilters,
    get_transaction_row,
    search_transactions,
    user_accounts,
)
from app.categorization.category_manager import assign_user_transaction_category
from app.db.models import AuditAction, Category, CategoryLevel, TransactionType
from app.web.deps import DB, USER, WebUser, notify_change
from app.web.render import audit, hx_trigger, is_htmx, parse_date, parse_float, parse_int, render

router = APIRouter()
PAGE = 40

TYPE_LABELS = {
    TransactionType.expense: "Gasto",
    TransactionType.income: "Ingreso",
    TransactionType.investment: "Inversión",
    TransactionType.unknown: "Otro",
}


def _filters(params) -> TransactionFilters:
    txn_type = params.get("tipo")
    category = params.get("categoria")
    return TransactionFilters(
        text=(params.get("q") or "")[:100],
        txn_type=TransactionType(txn_type) if txn_type in TransactionType._value2member_map_ else None,
        category_id=UNCATEGORIZED if category == "sin" else parse_int(category),
        account_id=parse_int(params.get("cuenta")),
        date_from=parse_date(params.get("desde")),
        date_to=parse_date(params.get("hasta")),
        min_amount=parse_float(params.get("min")),
        max_amount=parse_float(params.get("max")),
        category_label=(params.get("cat_nombre") or None),
    )


def _query_string(params, **overrides) -> str:
    keys = ("q", "tipo", "categoria", "cuenta", "desde", "hasta", "min", "max", "cat_nombre")
    values = {k: params.get(k) for k in keys if params.get(k)}
    values.update({k: v for k, v in overrides.items() if v is not None})
    return urlencode(values)


def category_groups(db: Session, user_id: int) -> "OrderedDict[str, List[Category]]":
    """Assignable categories (level 'category') grouped by their type name."""
    categories = (
        db.query(Category)
        .filter(Category.user_id == user_id, Category.level == CategoryLevel.category)
        .order_by(Category.name).all()
    )
    parents: Dict[int, str] = dict(
        db.query(Category.id, Category.name).filter(Category.user_id == user_id).all()
    )
    groups: "OrderedDict[str, List[Category]]" = OrderedDict()
    for cat in sorted(categories, key=lambda c: (parents.get(c.parent_id, "Otras"), c.name)):
        groups.setdefault(parents.get(cat.parent_id, "Otras"), []).append(cat)
    return groups


def _list_context(request: Request, db: Session, user: WebUser) -> dict:
    params = request.query_params
    offset = max(0, parse_int(params.get("offset")) or 0)
    filters = _filters(params)
    rows, total = search_transactions(db, user.id, filters, offset, PAGE)
    next_offset = offset + PAGE if offset + PAGE < total else None
    return {
        "rows": rows,
        "total": total,
        "offset": offset,
        "next_url": (f"/movimientos/lista?{_query_string(params, offset=next_offset)}"
                     if next_offset is not None else None),
        "type_labels": TYPE_LABELS,
    }


@router.get("/movimientos")
def transactions_page(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    params = request.query_params
    ctx = _list_context(request, db, user)
    ctx.update({
        "params": params,
        "groups": category_groups(db, user.id),
        "accounts": user_accounts(db, user.id),
        "types": [(t.value, label) for t, label in TYPE_LABELS.items()],
        "has_filters": bool(_query_string(params)),
        "show_filters": any(params.get(k) for k in ("tipo", "categoria", "cuenta", "desde", "hasta", "min", "max")),
    })
    return render(request, "movimientos.html", ctx, user=user, active="movimientos")


@router.get("/movimientos/lista")
def transactions_rows(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    return render(request, "partials/tx_rows.html", _list_context(request, db, user), user=user)


@router.get("/movimientos/{transaction_id}")
def transaction_detail(
    request: Request, transaction_id: int, user: WebUser = USER, db: Session = DB
) -> Response:
    row = get_transaction_row(db, user.id, transaction_id)
    if row is None:
        raise HTTPException(status_code=404)
    groups = category_groups(db, user.id)
    # The groups of the transaction's own kind (GASTO for an expense…) first.
    own = {
        name for (name,) in db.query(Category.name).filter(
            Category.user_id == user.id, Category.level == CategoryLevel.type,
            Category.transaction_type == row.txn_type)
    }
    ordered = OrderedDict(sorted(groups.items(), key=lambda item: item[0] not in own))
    return render(request, "partials/tx_sheet.html", {
        "row": row,
        "groups": ordered,
        "total_categories": sum(len(c) for c in groups.values()),
        "type_labels": TYPE_LABELS,
    }, user=user)


@router.post("/movimientos/{transaction_id}/categoria")
def set_category(
    request: Request,
    transaction_id: int,
    category_id: str = Form(""),
    user: WebUser = USER,
    db: Session = DB,
) -> Response:
    new_id = parse_int(category_id)
    before = get_transaction_row(db, user.id, transaction_id)
    if before is None:
        raise HTTPException(status_code=404)
    try:
        assign_user_transaction_category(db, user.id, transaction_id, new_id)
    except LookupError:
        raise HTTPException(status_code=404)
    audit(db, request, user, "transactions", transaction_id, AuditAction.update,
          old=f"category_id={before.category_id}", new=f"category_id={new_id} (manual)")
    notify_change(request, user, "transactions")
    row = get_transaction_row(db, user.id, transaction_id)
    if not is_htmx(request):
        return JSONResponse({"id": row.id, "category_id": row.category_id, "category": row.category_name})
    # Out-of-band swap: updates the row wherever the list is on screen and
    # is simply ignored on pages without it (Resumen).
    response = render(request, "partials/tx_row.html",
                      {"row": row, "type_labels": TYPE_LABELS, "oob": True}, user=user)
    hx_trigger(response, toast="Categoría guardada", closeSheet=True)
    return response


@router.get("/api/movimientos")
def transactions_api(request: Request, user: WebUser = USER, db: Session = DB) -> JSONResponse:
    params = request.query_params
    offset = max(0, parse_int(params.get("offset")) or 0)
    limit = min(200, max(1, parse_int(params.get("limit")) or PAGE))
    rows, total = search_transactions(db, user.id, _filters(params), offset, limit)
    return JSONResponse({
        "total": total,
        "rows": [
            {"id": r.id, "date": r.transaction_date.isoformat(), "description": r.description,
             "amount": r.amount, "type": r.txn_type.value, "category_id": r.category_id,
             "category": r.category_name, "account": r.account_name}
            for r in rows
        ],
    })
