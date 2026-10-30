"""Ajustes hub, accounts and import history (categories and rules: routers/admin.py;
family members: routers/users.py)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.categorization.rules_engine import rule_scope
from app.config import DEMO_USERNAME
from app.db.models import (
    Account,
    AutoCategorizationRule,
    Category,
    CategoryLevel,
    ImportBatch,
    Transaction,
    User,
    TRANSACTIONS_NEWEST_FIRST,
)
from app.web.deps import DB, USER, WebUser
from app.web.render import render
from app.web.routers.users import can_manage_users, family_members

router = APIRouter()


@router.get("/mas")
def more(request: Request, bienvenida: str = "", user: WebUser = USER, db: Session = DB) -> Response:
    counts = {
        "categorias": db.query(func.count(Category.id)).filter(
            Category.user_id == user.id, Category.level != CategoryLevel.type).scalar() or 0,
        "reglas": db.query(func.count(AutoCategorizationRule.id)).filter(
            rule_scope(db, user.id)).scalar() or 0,
        "cuentas": db.query(func.count(Account.id)).filter(Account.user_id == user.id).scalar() or 0,
        "importaciones": db.query(func.count(ImportBatch.id)).filter(ImportBatch.user_id == user.id).scalar() or 0,
    }
    last_import = (
        db.query(func.max(ImportBatch.import_timestamp)).filter(ImportBatch.user_id == user.id).scalar()
    )
    sessions = [s for s in request.app.state.sessions.active() if s.user_id == user.id]
    # Never list family names to the demo account (shown to visitors), and
    # it cannot change its password, name or delete itself.
    family = can_manage_users(db, user)
    members = family_members(db) if family else None
    me = db.get(User, user.id)
    invites = [
        {"id": inv.id, "expires_at": datetime.fromtimestamp(inv.expires)}
        for inv in request.app.state.invites.active_by(user.id)
    ] if family else []
    return render(request, "mas.html", {"counts": counts, "sessions": sessions, "last_import": last_import,
                                        "members": members, "demo_name": DEMO_USERNAME, "family": family,
                                        "member_since": me.created_at if me else None, "invites": invites,
                                        "welcome": bienvenida == "1" and not counts["categorias"]},
                  user=user, active="mas")


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
            .order_by(*TRANSACTIONS_NEWEST_FIRST)
            .limit(1).scalar()
        )
    return render(request, "mas_cuentas.html", {"accounts": rows, "stats": stats, "balances": balances},
                  user=user, active="cuentas")


@router.get("/mas/importaciones")
def imports(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    rows = (
        db.query(ImportBatch).filter(ImportBatch.user_id == user.id)
        .order_by(ImportBatch.import_timestamp.desc()).limit(200).all()
    )
    return render(request, "mas_importaciones.html", {"batches": rows}, user=user, active="importar")
