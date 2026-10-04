"""Administración: categories and auto-categorization rules (full CRUD).

Every write checks ownership in app.categorization.admin_service, is
audited and refreshes the desktop app (notify_change "transactions").
Forms open in the bottom sheet; on success the page reloads in place.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.categorization.admin_service import (
    AdminError,
    NotFound,
    add_category,
    add_rule,
    apply_all_rules,
    category_usage,
    edit_rule,
    load_defaults,
    move_rule,
    own_category,
    own_rule,
    preview_pattern,
    remove_category,
    remove_rule,
    rename_category,
    rule_match_counts,
    visible_rules,
)
from app.db.models import AuditAction, Category, CategoryLevel, MatchType, Transaction
from app.web.deps import DB, USER, WebUser, notify_change
from app.web.render import audit, hx_trigger, is_htmx, parse_int, render

router = APIRouter()

MATCH_TYPES = [
    (MatchType.contains.value, "Contiene"),
    (MatchType.exact.value, "Es exactamente"),
    (MatchType.regex.value, "Expresión regular"),
]
MATCH_LABELS = dict(MATCH_TYPES)


def _done(request: Request, message: str) -> Response:
    """Write succeeded: toast, close the sheet, reload the page in place."""
    response = Response(status_code=204)
    if is_htmx(request):
        hx_trigger(response, toast=message, closeSheet=True, refreshPage=True)
    else:
        response = Response(status_code=303, headers={"Location": request.headers.get("referer") or "/mas"})
    return response


def _error_toast(message: str) -> Response:
    response = Response(status_code=204)
    hx_trigger(response, toastError=message)
    return response


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def _tree(db: Session, user_id: int):
    rows = db.query(Category).filter(Category.user_id == user_id).order_by(Category.name).all()
    children: Dict[int, List[Category]] = defaultdict(list)
    for cat in rows:
        if cat.parent_id is not None:
            children[cat.parent_id].append(cat)
    types = [c for c in rows if c.level == CategoryLevel.type]
    return types, children


def category_options(db: Session, user_id: int) -> "OrderedDict[str, List[Category]]":
    """Assignable categories grouped by type name (for <optgroup>s)."""
    types, children = _tree(db, user_id)
    groups: "OrderedDict[str, List[Category]]" = OrderedDict()
    for t in types:
        if children[t.id]:
            groups[t.name] = children[t.id]
    return groups


@router.get("/mas/categorias")
def categories(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    types, children = _tree(db, user.id)
    usage: Dict[int, int] = dict(
        db.query(Transaction.category_id, func.count(Transaction.id))
        .filter(Transaction.user_id == user.id, Transaction.category_id.isnot(None))
        .group_by(Transaction.category_id).all()
    )
    totals = {t.id: sum(usage.get(c.id, 0) for c in children[t.id]) for t in types}
    return render(request, "mas_categorias.html", {
        "types": types, "children": children, "usage": usage, "totals": totals,
    }, user=user, active="categorias")


@router.get("/mas/categorias/nueva")
def category_new_form(request: Request, tipo: Optional[int] = None, user: WebUser = USER,
                      db: Session = DB) -> Response:
    types, _ = _tree(db, user.id)
    return render(request, "partials/category_form.html", {
        "cat": None, "types": types, "parent_id": tipo, "name": "",
    }, user=user)


@router.post("/mas/categorias")
def category_create(request: Request, parent_id: str = Form(""), name: str = Form("", max_length=200),
                    user: WebUser = USER, db: Session = DB) -> Response:
    try:
        cat = add_category(db, user.id, parse_int(parent_id) or 0, name)
    except NotFound:
        error = "Elige el tipo (GASTO, INGRESO…)."
    except AdminError as exc:
        error = str(exc)
    else:
        audit(db, request, user, "categories", cat.id, AuditAction.insert, new=f"categoría «{cat.name}»")
        notify_change(request, user, "transactions")
        return _done(request, f"Categoría «{cat.name}» creada")
    types, _ = _tree(db, user.id)
    return render(request, "partials/category_form.html", {
        "cat": None, "types": types, "parent_id": parse_int(parent_id), "name": name, "error": error,
    }, user=user)


@router.get("/mas/categorias/{category_id}")
def category_edit_form(request: Request, category_id: int, user: WebUser = USER, db: Session = DB) -> Response:
    try:
        cat = own_category(db, user.id, category_id)
    except NotFound:
        raise HTTPException(status_code=404)
    usage = category_usage(db, user.id, cat.id) if cat.level != CategoryLevel.type else None
    return render(request, "partials/category_form.html", {
        "cat": cat, "name": cat.name, "usage": usage,
    }, user=user)


@router.post("/mas/categorias/{category_id}/renombrar")
def category_rename(request: Request, category_id: int, name: str = Form("", max_length=200),
                    user: WebUser = USER, db: Session = DB) -> Response:
    try:
        cat = own_category(db, user.id, category_id)
        old = cat.name
        rename_category(db, user.id, category_id, name)
    except NotFound:
        raise HTTPException(status_code=404)
    except AdminError as exc:
        usage = category_usage(db, user.id, cat.id) if cat.level != CategoryLevel.type else None
        return render(request, "partials/category_form.html", {
            "cat": cat, "name": name, "usage": usage, "error": str(exc),
        }, user=user)
    audit(db, request, user, "categories", category_id, AuditAction.update,
          old=f"«{old}»", new=f"«{cat.name}»")
    notify_change(request, user, "transactions")
    return _done(request, "Categoría renombrada")


@router.post("/mas/categorias/{category_id}/borrar")
def category_delete(request: Request, category_id: int, user: WebUser = USER, db: Session = DB) -> Response:
    try:
        cat = own_category(db, user.id, category_id)
        name = cat.name
        usage = remove_category(db, user.id, category_id)
    except NotFound:
        raise HTTPException(status_code=404)
    except AdminError as exc:
        return _error_toast(str(exc))
    audit(db, request, user, "categories", category_id, AuditAction.delete,
          old=f"«{name}» ({usage.transactions} movimientos, {usage.rules} reglas)")
    notify_change(request, user, "transactions")
    return _done(request, "Categoría borrada")


@router.post("/mas/categorias/predeterminadas")
def category_defaults(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    try:
        created, rules_created = load_defaults(db, user.id)
    except ValueError as exc:
        db.rollback()
        return _error_toast(str(exc))
    audit(db, request, user, "categories", None, AuditAction.insert,
          new=f"categorías por defecto: {created} categorías, {rules_created} reglas")
    notify_change(request, user, "transactions")
    if created or rules_created:
        return _done(request, f"{created} categorías y {rules_created} reglas creadas")
    return _done(request, "Ya estaban todas creadas")


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@router.get("/mas/reglas")
def rules(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    rows = visible_rules(db, user.id)
    names = dict(
        db.query(Category.id, Category.name).filter(Category.id.in_({r.category_id for r in rows})).all()
    ) if rows else {}
    uncategorized = db.query(func.count(Transaction.id)).filter(
        Transaction.user_id == user.id, Transaction.category_id.is_(None)).scalar() or 0
    return render(request, "mas_reglas.html", {
        "rules": rows, "names": names, "match_labels": MATCH_LABELS,
        "counts": rule_match_counts(db, user.id), "uncategorized": uncategorized,
        "first_id": rows[0].id if rows else None, "last_id": rows[-1].id if rows else None,
    }, user=user, active="reglas")


def _rule_form(request: Request, db: Session, user: WebUser, rule=None, values=None, error=None) -> Response:
    values = values or {}
    if rule is not None and not values:
        values = {"category_id": rule.category_id, "pattern": rule.match_pattern,
                  "match_type": rule.match_type.value, "priority": rule.priority}
    values.setdefault("match_type", MatchType.contains.value)
    values.setdefault("priority", 100)
    return render(request, "partials/rule_form.html", {
        "rule": rule, "v": values, "groups": category_options(db, user.id),
        "match_types": MATCH_TYPES, "error": error,
    }, user=user)


@router.get("/mas/reglas/nueva")
def rule_new_form(request: Request, patron: str = "", categoria: Optional[int] = None,
                  user: WebUser = USER, db: Session = DB) -> Response:
    return _rule_form(request, db, user, values={"pattern": patron[:200], "category_id": categoria})


@router.get("/mas/reglas/probar")
def rule_preview(request: Request, pattern: str = "", match_type: str = "contains",
                 user: WebUser = USER, db: Session = DB) -> Response:
    preview = preview_pattern(db, user.id, pattern[:200], match_type)
    return render(request, "partials/rule_preview.html", {"p": preview, "pattern": pattern}, user=user)


@router.get("/mas/reglas/{rule_id}")
def rule_edit_form(request: Request, rule_id: int, user: WebUser = USER, db: Session = DB) -> Response:
    try:
        rule = own_rule(db, user.id, rule_id)
    except NotFound:
        raise HTTPException(status_code=404)
    except AdminError as exc:
        return _error_toast(str(exc))
    return _rule_form(request, db, user, rule=rule)


@router.post("/mas/reglas")
def rule_save(request: Request, rule_id: str = Form(""), category_id: str = Form(""),
              pattern: str = Form("", max_length=400), match_type: str = Form("contains"),
              priority: str = Form("100"), user: WebUser = USER, db: Session = DB) -> Response:
    values = {"category_id": parse_int(category_id), "pattern": pattern, "match_type": match_type,
              "priority": priority}
    existing = parse_int(rule_id)
    rule = None
    try:
        if existing is not None:
            rule = own_rule(db, user.id, existing)
            before = f"«{rule.match_pattern}» {rule.match_type.value} → cat {rule.category_id} p{rule.priority}"
            saved = edit_rule(db, user.id, existing, values["category_id"], pattern, match_type,
                              parse_int(priority))
        else:
            before = None
            saved = add_rule(db, user.id, values["category_id"], pattern, match_type, parse_int(priority))
    except NotFound:
        if rule is None and existing is not None:
            raise HTTPException(status_code=404)
        return _rule_form(request, db, user, rule=rule, values=values, error="Elige una categoría válida.")
    except AdminError as exc:
        return _rule_form(request, db, user, rule=rule, values=values, error=str(exc))
    audit(db, request, user, "auto_categorization_rules", saved.id,
          AuditAction.update if existing else AuditAction.insert, old=before,
          new=f"«{saved.match_pattern}» {saved.match_type.value} → cat {saved.category_id} p{saved.priority}")
    notify_change(request, user, "transactions")
    return _done(request, "Regla guardada · aplica las reglas para usarla")


@router.post("/mas/reglas/{rule_id}/borrar")
def rule_delete(request: Request, rule_id: int, user: WebUser = USER, db: Session = DB) -> Response:
    try:
        rule = own_rule(db, user.id, rule_id)
        summary = f"«{rule.match_pattern}» → cat {rule.category_id}"
        remove_rule(db, user.id, rule_id)
    except NotFound:
        raise HTTPException(status_code=404)
    except AdminError as exc:
        return _error_toast(str(exc))
    audit(db, request, user, "auto_categorization_rules", rule_id, AuditAction.delete, old=summary)
    notify_change(request, user, "transactions")
    return _done(request, "Regla borrada")


@router.post("/mas/reglas/{rule_id}/prioridad")
def rule_move(request: Request, rule_id: int, direccion: str = Form(""),
              user: WebUser = USER, db: Session = DB) -> Response:
    try:
        old = own_rule(db, user.id, rule_id).priority
        rule = move_rule(db, user.id, rule_id, direccion)
    except NotFound:
        raise HTTPException(status_code=404)
    except AdminError as exc:
        return _error_toast(str(exc))
    if rule.priority != old:
        audit(db, request, user, "auto_categorization_rules", rule_id, AuditAction.update,
              old=f"priority={old}", new=f"priority={rule.priority}")
        notify_change(request, user, "transactions")
    response = Response(status_code=204)
    if is_htmx(request):
        hx_trigger(response, refreshPage=True)
    return response


@router.post("/mas/reglas/aplicar")
def rules_apply(request: Request, modo: str = Form("sin"), user: WebUser = USER, db: Session = DB) -> Response:
    overwrite = modo == "todas"
    summary = apply_all_rules(db, user.id, overwrite=overwrite)
    audit(db, request, user, "transactions", None, AuditAction.update,
          new=f"reglas aplicadas ({'todas' if overwrite else 'sin categoría'}): "
              f"{summary.assigned} asignadas, {summary.conflicts} conflictos")
    notify_change(request, user, "transactions")
    message = f"{summary.assigned} movimientos categorizados"
    if summary.conflicts:
        message += f" · {summary.conflicts} con conflicto"
    return _done(request, message)
