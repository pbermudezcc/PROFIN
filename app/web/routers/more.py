"""Más: categories, rules, accounts, import history, sessions (read-only for now)."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    AutoCategorizationRule,
    Category,
    CategoryLevel,
    ImportBatch,
    Transaction,
)
from app.web.deps import DB, USER, WebUser
from app.web.render import render

router = APIRouter()

MATCH_LABELS = {"contains": "contiene", "exact": "es exactamente", "regex": "expresión regular"}


@router.get("/mas")
def more(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    counts = {
        "categorias": db.query(func.count(Category.id)).filter(
            Category.user_id == user.id, Category.level != CategoryLevel.type).scalar() or 0,
        "reglas": db.query(func.count(AutoCategorizationRule.id)).filter(
            or_(AutoCategorizationRule.user_id == user.id, AutoCategorizationRule.user_id.is_(None))
        ).scalar() or 0,
        "cuentas": db.query(func.count(Account.id)).filter(Account.user_id == user.id).scalar() or 0,
        "importaciones": db.query(func.count(ImportBatch.id)).filter(ImportBatch.user_id == user.id).scalar() or 0,
    }
    sessions = [s for s in request.app.state.sessions.active() if s.user_id == user.id]
    return render(request, "mas.html", {"counts": counts, "sessions": sessions},
                  user=user, active="mas")


@router.get("/mas/categorias")
def categories(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    rows = db.query(Category).filter(Category.user_id == user.id).order_by(Category.name).all()
    usage: Dict[int, int] = dict(
        db.query(Transaction.category_id, func.count(Transaction.id))
        .filter(Transaction.user_id == user.id, Transaction.category_id.isnot(None))
        .group_by(Transaction.category_id).all()
    )
    children: Dict[int, List[Category]] = defaultdict(list)
    for cat in rows:
        if cat.parent_id is not None:
            children[cat.parent_id].append(cat)
    types = [c for c in rows if c.level == CategoryLevel.type]
    return render(request, "mas_categorias.html", {
        "types": types, "children": children, "usage": usage,
    }, user=user, active="mas")


@router.get("/mas/reglas")
def rules(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    rows = (
        db.query(AutoCategorizationRule)
        .filter(or_(AutoCategorizationRule.user_id == user.id, AutoCategorizationRule.user_id.is_(None)))
        .order_by(AutoCategorizationRule.priority, AutoCategorizationRule.id).all()
    )
    names = dict(
        db.query(Category.id, Category.name)
        .filter(Category.id.in_({r.category_id for r in rows})).all()
    )
    return render(request, "mas_reglas.html", {
        "rules": rows, "names": names, "match_labels": MATCH_LABELS,
    }, user=user, active="mas")


@router.get("/mas/cuentas")
def accounts(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    rows = db.query(Account).filter(Account.user_id == user.id).order_by(Account.account_name).all()
    stats = {
        account_id: (count, first, last)
        for account_id, count, first, last in db.query(
            Transaction.account_id, func.count(Transaction.id),
            func.min(Transaction.transaction_date), func.max(Transaction.transaction_date),
        ).filter(Transaction.user_id == user.id).group_by(Transaction.account_id)
    }
    balances = {}
    for account in rows:
        balances[account.id] = (
            db.query(Transaction.running_balance)
            .filter(Transaction.user_id == user.id, Transaction.account_id == account.id,
                    Transaction.running_balance.isnot(None))
            .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
            .limit(1).scalar()
        )
    return render(request, "mas_cuentas.html", {"accounts": rows, "stats": stats, "balances": balances},
                  user=user, active="mas")


@router.get("/mas/importaciones")
def imports(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    rows = (
        db.query(ImportBatch).filter(ImportBatch.user_id == user.id)
        .order_by(ImportBatch.import_timestamp.desc()).limit(200).all()
    )
    return render(request, "mas_importaciones.html", {"batches": rows}, user=user, active="mas")
