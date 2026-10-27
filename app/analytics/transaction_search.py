"""
app/analytics/transaction_search.py
===================================
FAMILYFIN — Filtered, paginated list of one user's bank transactions
(web Movimientos list and desktop Transacciones table).

Free-text search ("buscar en todos los campos"): the text is split into
words; every word must appear (substring, case- and accent-insensitive) in
at least one field of the transaction — AND across words, OR across fields.
Searched fields: every concept-like column the importer stores
(``CONCEPT_COLUMNS``: issuer_name, concept, concepto_comun, concepto_propio,
raw_col14, raw_details), operation/value dates, amount and running balance
in the forms a user types (``1.234,56`` / ``1234.56`` / ``-45``), type
label, category and its parent type, category source, account (name, bank,
IBAN last 4) and source file name.

Matching runs in Python over the rows that pass the structured filters
(type, category, account, dates, amounts — all applied in SQL first). Only
the id and the searched columns are loaded (no ORM objects), so a few
thousand rows filter in tens of milliseconds, and user input never reaches
the SQL text.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    CATEGORY_SOURCE_MANUAL,
    CATEGORY_SOURCE_RULE,
    Account,
    Category,
    Transaction,
    TransactionType,
)

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
    return query


# ---------------------------------------------------------------------------
# Free-text search over all fields
# ---------------------------------------------------------------------------

SEARCH_TYPE_LABELS = {
    TransactionType.income:     "Ingreso",
    TransactionType.expense:    "Gasto",
    TransactionType.investment: "Inversión",
    TransactionType.unknown:    "Otro",
}
_SOURCE_LABELS = {
    CATEGORY_SOURCE_MANUAL: "manual",
    CATEGORY_SOURCE_RULE:   "regla automática",
}

# Every concept-like column the importer stores, shown in the UI or not.
CONCEPT_COLUMNS = (
    Transaction.issuer_name,
    Transaction.concept,
    Transaction.concepto_comun,
    Transaction.concepto_propio,
    Transaction.raw_col14,
    Transaction.raw_details,
)


def fold_text(value: str) -> str:
    """Lower-case and strip accents ("Alimentación" -> "alimentacion")."""
    if value.isascii():               # most bank text: skip normalization
        return value.lower()
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def search_terms(text: Optional[str]) -> List[str]:
    """Folded, de-duplicated words of the search box (order kept)."""
    terms: List[str] = []
    for word in (text or "").split():
        folded = fold_text(word)
        if folded and folded not in terms:
            terms.append(folded)
    return terms


def _amount_variants(value: Optional[float]) -> List[str]:
    """How a user may type an amount: Spanish or dotted decimals, with or
    without thousands separator, unsigned and with its sign."""
    if value is None:
        return []
    value = round(float(value), 2)
    plain = f"{abs(value):.2f}"                       # 1234.56
    grouped = f"{abs(value):,.2f}"                    # 1,234.56
    es_grouped = grouped.replace(",", "X").replace(".", ",").replace("X", ".")
    forms = [plain, plain.replace(".", ","), es_grouped, grouped]
    sign = "-" if value < 0 else "+"
    return forms + [sign + form for form in forms]


def _date_variants(value: Optional[date]) -> List[str]:
    if value is None:
        return []
    return [value.isoformat(), value.strftime("%d/%m/%Y"), value.strftime("%d-%m-%Y")]


def matching_transaction_ids(session: Session, user_id: int, query, text: str) -> List[int]:
    """Ids (newest first) of the rows of ``query`` where every word of
    ``text`` appears in at least one field. ``query`` must be a Transaction
    query already scoped to ``user_id``."""
    terms = search_terms(text)
    rows = (
        query.with_entities(
            Transaction.id,
            Transaction.transaction_date,
            Transaction.value_date,
            Transaction.amount,
            Transaction.running_balance,
            Transaction.type,
            Transaction.category_id,
            Transaction.category_source,
            Transaction.account_id,
            Transaction.source_file,
            *CONCEPT_COLUMNS,
        )
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .all()
    )
    if not terms:
        return [row[0] for row in rows]

    cats = (
        session.query(Category.id, Category.name, Category.parent_id)
        .filter(Category.user_id == user_id).all()
    )
    cat_names = {cid: name for cid, name, _ in cats}
    cat_text = {
        cid: fold_text(" ".join(filter(None, (name, cat_names.get(parent)))))
        for cid, name, parent in cats
    }
    uncategorized = fold_text(UNCATEGORIZED_LABEL)
    account_text = {
        aid: fold_text(" ".join(filter(None, (name, bank, last4))))
        for aid, name, bank, last4 in session.query(
            Account.id, Account.account_name, Account.bank, Account.iban_last4
        ).filter(Account.user_id == user_id)
    }
    type_text = {t: fold_text(f"{label} {t.value}") for t, label in SEARCH_TYPE_LABELS.items()}

    ids: List[int] = []
    for (txn_id, txn_date, value_date, amount, balance, txn_type, category_id,
         category_source, account_id, source_file, *concepts) in rows:
        parts: List[str] = [fold_text(c) for c in concepts if c]
        parts += _date_variants(txn_date) + _date_variants(value_date)
        parts += _amount_variants(amount) + _amount_variants(balance)
        parts.append(type_text.get(TransactionType(txn_type), ""))
        parts.append(cat_text.get(category_id, "") if category_id else uncategorized)
        parts.append(_SOURCE_LABELS.get(category_source, ""))
        parts.append(account_text.get(account_id, ""))
        if source_file:
            parts.append(fold_text(source_file))
        haystack = "\n".join(parts)
        if all(term in haystack for term in terms):
            ids.append(txn_id)
    return ids


def filtered_transactions_page(
    session: Session, user_id: int, query, text: str, offset: int, limit: int,
) -> Tuple[List[Transaction], int]:
    """(ORM rows newest first, total matching) for one page of ``query``
    after the free-text search. Without search words paging stays in SQL."""
    offset = max(0, offset)
    if not search_terms(text):
        total = query.count()
        txns = (
            query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
            .offset(offset).limit(limit).all()
        )
        return txns, total
    ids = matching_transaction_ids(session, user_id, query, text)
    page_ids = ids[offset:offset + limit]
    if not page_ids:
        return [], len(ids)
    by_id = {
        t.id: t for t in session.query(Transaction).filter(
            Transaction.user_id == user_id, Transaction.id.in_(page_ids))
    }
    return [by_id[i] for i in page_ids if i in by_id], len(ids)


def search_transactions(
    session: Session,
    user_id: int,
    filters: TransactionFilters,
    offset: int = 0,
    limit: int = 40,
) -> Tuple[List[TransactionRow], int]:
    """(rows newest first, total matching) for one page."""
    query = _query(session, user_id, filters)
    txns, total = filtered_transactions_page(session, user_id, query, filters.text, offset, limit)
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
