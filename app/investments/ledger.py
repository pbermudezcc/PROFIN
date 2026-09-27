"""
app/investments/ledger.py
=========================
FAMILYFIN — Create, edit and delete investment operations safely.

Every write goes through here so the ledger can never become impossible:
before saving, the user's operations of the affected security are replayed
with the change applied (portfolio.ledger_error), and a change that would
leave a later sale without shares is rejected with a Spanish message. The
same applies to deletions (removing an old buy can break a later sale).

All reads and writes are scoped to one user: an operation id of another
family member is treated as not found.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import InvestmentKind, InvestmentTransaction, Security
from app.investments.portfolio import LedgerError, ledger_error, validate_fields
from app.investments.prices import reset_price_history

KIND_LABELS = {
    InvestmentKind.buy:      "Compra",
    InvestmentKind.sell:     "Venta",
    InvestmentKind.dividend: "Dividendo",
    InvestmentKind.split:    "Split",
    InvestmentKind.grant:    "Plan de empresa",
}

# Offered in the broker box before the user has typed any of their own.
DEFAULT_BROKERS = (
    "DEGIRO",
    "Trade Republic",
    "MyInvestor",
    "Scalable Capital",
    "EquatePlus",
    "Tressis",
)


@dataclass
class TransactionDraft:
    """What the entry form produces; shaped like InvestmentTransaction."""
    security_id:      int
    kind:             InvestmentKind
    trade_date:       date
    broker:           str
    quantity:         Optional[float] = None
    price:            Optional[float] = None
    currency:         Optional[str] = None
    amount_eur:       float = 0.0
    fees_eur:         float = 0.0
    tax_withheld_eur: float = 0.0
    cash_paid_eur:    Optional[float] = None
    split_ratio:      Optional[float] = None
    notes:            Optional[str] = None
    id:               Optional[int] = None     # set when editing


_FIELDS = (
    "security_id", "kind", "trade_date", "broker", "quantity", "price", "currency",
    "amount_eur", "fees_eur", "tax_withheld_eur", "cash_paid_eur", "split_ratio", "notes",
)


def _normalized(draft: TransactionDraft) -> TransactionDraft:
    """Clear the fields a kind does not use and round money to cents."""
    kind = InvestmentKind(draft.kind)
    cents = lambda v: round(float(v or 0.0), 2)   # noqa: E731
    clean = replace(
        draft,
        kind=kind,
        broker=(draft.broker or "").strip(),
        notes=(draft.notes or "").strip() or None,
        amount_eur=cents(draft.amount_eur),
        fees_eur=cents(draft.fees_eur),
        tax_withheld_eur=cents(draft.tax_withheld_eur),
        cash_paid_eur=None if draft.cash_paid_eur is None else cents(draft.cash_paid_eur),
    )
    if kind != InvestmentKind.grant:
        clean.cash_paid_eur = None
    if kind != InvestmentKind.dividend:
        clean.tax_withheld_eur = 0.0
    if kind != InvestmentKind.split:
        clean.split_ratio = None
    if kind == InvestmentKind.split:
        clean.quantity = clean.price = None
        clean.amount_eur = clean.fees_eur = 0.0
    if kind == InvestmentKind.dividend:
        clean.quantity = clean.price = None
    return clean


def _user_ledger(session: Session, user_id: int, security_id: int) -> List[InvestmentTransaction]:
    return (
        session.query(InvestmentTransaction)
        .filter(
            InvestmentTransaction.user_id == user_id,
            InvestmentTransaction.security_id == security_id,
        )
        .all()
    )


def _check(ledger) -> None:
    error = ledger_error(ledger)
    if error is not None:
        raise error


def get_transaction(session: Session, user_id: int, transaction_id: int) -> InvestmentTransaction:
    txn = session.get(InvestmentTransaction, transaction_id)
    if txn is None or txn.user_id != user_id:
        raise LedgerError("La operación no existe o pertenece a otro usuario.")
    return txn


def save_transaction(
    session: Session, user_id: int, draft: TransactionDraft
) -> InvestmentTransaction:
    """Insert (draft.id is None) or update an operation after checking the ledger.

    Raises LedgerError (Spanish message) and leaves the database untouched
    when the operation is invalid or would make another one impossible.
    """
    draft = _normalized(draft)
    if not draft.broker:
        raise LedgerError("Indica el bróker de la operación.")
    if session.get(Security, draft.security_id) is None:
        raise LedgerError("Selecciona un valor.")
    errors = validate_fields(draft)
    if errors:
        raise LedgerError(errors[0])

    existing = None
    if draft.id is not None:
        existing = get_transaction(session, user_id, draft.id)
        if existing.security_id != draft.security_id:
            # Moving it to another security must not break the old one.
            _check([t for t in _user_ledger(session, user_id, existing.security_id)
                    if t.id != existing.id])

    others = [t for t in _user_ledger(session, user_id, draft.security_id) if t.id != draft.id]
    _check(others + [draft])

    touches_split = draft.kind == InvestmentKind.split or (
        existing is not None and InvestmentKind(existing.kind) == InvestmentKind.split)
    old_security_id = existing.security_id if existing is not None else None
    txn = existing or InvestmentTransaction(user_id=user_id)
    for name in _FIELDS:
        setattr(txn, name, getattr(draft, name))
    if existing is None:
        session.add(txn)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    if touches_split:
        # Cached closes were stored before the provider adjusted them for
        # this split: download that security's history again.
        for security_id in {draft.security_id, old_security_id} - {None}:
            reset_price_history(session, security_id)
    return txn


def delete_transaction(session: Session, user_id: int, transaction_id: int) -> None:
    """Delete an operation unless a later one depends on it."""
    txn = get_transaction(session, user_id, transaction_id)
    remaining = [t for t in _user_ledger(session, user_id, txn.security_id) if t.id != txn.id]
    error = ledger_error(remaining)
    if error is not None:
        raise LedgerError(f"No se puede borrar esta operación. {error}", error.transaction)
    was_split = InvestmentKind(txn.kind) == InvestmentKind.split
    security_id = txn.security_id
    session.delete(txn)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    if was_split:
        reset_price_history(session, security_id)


def list_transactions(
    session: Session,
    user_id: int,
    kind: Optional[InvestmentKind] = None,
    broker: Optional[str] = None,
    text: Optional[str] = None,
) -> List[InvestmentTransaction]:
    """The user's operations, newest first, with optional filters."""
    query = (
        session.query(InvestmentTransaction)
        .join(Security, Security.id == InvestmentTransaction.security_id)
        .filter(InvestmentTransaction.user_id == user_id)
    )
    if kind is not None:
        query = query.filter(InvestmentTransaction.kind == kind)
    if broker:
        query = query.filter(InvestmentTransaction.broker == broker)
    if text:
        like = f"%{text.strip()}%"
        query = query.filter(
            Security.symbol.ilike(like) | Security.name.ilike(like)
            | InvestmentTransaction.notes.ilike(like)
        )
    return query.order_by(
        InvestmentTransaction.trade_date.desc(), InvestmentTransaction.id.desc()
    ).all()


def known_brokers(session: Session, user_id: int) -> List[str]:
    """The user's brokers, most recently used first, then the defaults."""
    rows = (
        session.query(InvestmentTransaction.broker, func.max(InvestmentTransaction.trade_date))
        .filter(InvestmentTransaction.user_id == user_id)
        .group_by(InvestmentTransaction.broker)
        .order_by(func.max(InvestmentTransaction.trade_date).desc())
        .all()
    )
    used = [broker for broker, _ in rows]
    return used + [b for b in DEFAULT_BROKERS if b not in used]


def picker_securities(session: Session, user_id: int) -> List[Security]:
    """Catalogue for the entry form: the user's own securities first, by name."""
    own_ids = {
        sid for (sid,) in session.query(InvestmentTransaction.security_id)
        .filter(InvestmentTransaction.user_id == user_id).distinct()
    }
    securities = session.query(Security).all()
    return sorted(securities, key=lambda s: (s.id not in own_ids, s.name.lower()))


def estimate_amount_eur(
    kind: InvestmentKind,
    quantity: float,
    price: float,
    fees_eur: float,
    fx_rate: Optional[float],
) -> Optional[float]:
    """Suggested EUR total for the form: shares × price ÷ FX, ± fees.

    Buys add the fees (money charged), sales subtract them (money received).
    None when the FX rate is unknown or there is nothing to multiply.
    """
    if not fx_rate or not quantity or not price:
        return None
    gross = quantity * price / fx_rate
    kind = InvestmentKind(kind)
    if kind == InvestmentKind.sell:
        return round(gross - (fees_eur or 0.0), 2)
    return round(gross + (fees_eur or 0.0), 2)
