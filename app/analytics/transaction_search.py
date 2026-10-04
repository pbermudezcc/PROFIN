"""
app/analytics/transaction_search.py
===================================
FAMILYFIN — Filtered, paginated list of one user's bank transactions
(web Movimientos list).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.models import Account, Category, Transaction, TransactionType

UNCATEGORIZED = -1
UNCATEGORIZED_LABEL = "Sin categorizar"


@dataclass
class TransactionFilters:
    text:        str = ""
    txn_type:    Optional[TransactionType] = None
    category_id: Optional[int] = None        # UNCATEGORIZED = only rows without one
    account_id:  Optional[int] = None
    date_from:   Optional[date] = None
    date_to:     Optional[date] = None
    min_amount:  Optional[float] = None      # on |amount|
    max_amount:  Optional[float] = None
    # Category name as the Gastos/Ingresos charts group them (same-named
    # categories share a slice); "Sin categorizar" = rows without one.
    category_label: Optional[str] = None


@dataclass
class TransactionRow:
    id:               int
    transaction_date: date
    value_date:       Optional[date]
    description:      str
    concept:          Optional[str]
    amount:           float
    running_balance:  Optional[float]
    txn_type:         TransactionType
    category_id:      Optional[int]
    category_name:    Optional[str]
    category_source:  Optional[str]
    account_name:     str


def _query(session: Session, user_id: int, filters: TransactionFilters):
    query = session.query(Transaction).filter(Transaction.user_id == user_id)
    if filters.txn_type is not None:
        query = query.filter(Transaction.type == filters.txn_type)
    if filters.category_id == UNCATEGORIZED:
        query = query.filter(Transaction.category_id.is_(None))
    elif filters.category_id is not None:
        query = query.filter(Transaction.category_id == filters.category_id)
    if filters.category_label:
        if filters.category_label == UNCATEGORIZED_LABEL:
            query = query.filter(Transaction.category_id.is_(None))
        else:
            ids = [
                cid for (cid,) in session.query(Category.id).filter(
                    Category.user_id == user_id, Category.name == filters.category_label)
            ]
            query = query.filter(Transaction.category_id.in_(ids))
    if filters.account_id is not None:
        query = query.filter(Transaction.account_id == filters.account_id)
    if filters.date_from is not None:
        query = query.filter(Transaction.transaction_date >= filters.date_from)
    if filters.date_to is not None:
        query = query.filter(Transaction.transaction_date <= filters.date_to)
    if filters.min_amount is not None:
        query = query.filter(func.abs(Transaction.amount) >= filters.min_amount)
    if filters.max_amount is not None:
        query = query.filter(func.abs(Transaction.amount) <= filters.max_amount)
    text = filters.text.strip()
    if text:
        like = f"%{text}%"
        query = query.filter(or_(Transaction.issuer_name.ilike(like), Transaction.concept.ilike(like)))
    return query


def search_transactions(
    session: Session,
    user_id: int,
    filters: TransactionFilters,
    offset: int = 0,
    limit: int = 40,
) -> Tuple[List[TransactionRow], int]:
    """(rows newest first, total matching) for one page."""
    query = _query(session, user_id, filters)
    total = query.count()
    txns = (
        query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .offset(max(0, offset)).limit(limit).all()
    )
    names: Dict[int, str] = dict(
        session.query(Category.id, Category.name).filter(Category.user_id == user_id).all()
    )
    accounts: Dict[int, str] = dict(
        session.query(Account.id, Account.account_name).filter(Account.user_id == user_id).all()
    )
    rows = [
        TransactionRow(
            id=t.id,
            transaction_date=t.transaction_date,
            value_date=t.value_date,
            description=t.issuer_name or t.concept or "—",
            concept=t.concept,
            amount=t.amount,
            running_balance=t.running_balance,
            txn_type=TransactionType(t.type),
            category_id=t.category_id,
            category_name=names.get(t.category_id),
            category_source=t.category_source,
            account_name=accounts.get(t.account_id, ""),
        )
        for t in txns
    ]
    return rows, total


def get_transaction_row(session: Session, user_id: int, transaction_id: int) -> Optional[TransactionRow]:
    txn = session.get(Transaction, transaction_id)
    if txn is None or txn.user_id != user_id:
        return None
    category = session.get(Category, txn.category_id) if txn.category_id else None
    account = session.get(Account, txn.account_id)
    return TransactionRow(
        id=txn.id,
        transaction_date=txn.transaction_date,
        value_date=txn.value_date,
        description=txn.issuer_name or txn.concept or "—",
        concept=txn.concept,
        amount=txn.amount,
        running_balance=txn.running_balance,
        txn_type=TransactionType(txn.type),
        category_id=txn.category_id,
        category_name=category.name if category and category.user_id == user_id else None,
        category_source=txn.category_source,
        account_name=account.account_name if account else "",
    )


def user_accounts(session: Session, user_id: int) -> List[Account]:
    return (
        session.query(Account).filter(Account.user_id == user_id)
        .order_by(Account.account_name).all()
    )
