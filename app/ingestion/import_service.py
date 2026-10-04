"""
app/ingestion/import_service.py
===============================
FAMILYFIN — CaixaBank statement import for one logged-in user.

Qt-free steps shared by the desktop ImportView and the web upload flow:
  1. ensure_default_account → the account statements go to.
  2. prepare_import (importer.py) → parse + validate + duplicate check.
  3. run_import → write the approved rows, then auto-categorize them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List

from sqlalchemy.orm import Session

from app.categorization.rules_engine import apply_rules_and_commit
from app.db.models import Account
from app.ingestion.importer import ImportPreview, ImportSummary, commit_import

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT_NAME = "CaixaBank Principal"


def ensure_default_account(session: Session, user_id: int) -> Account:
    """The user's first account, created on the very first import."""
    account = (
        session.query(Account).filter(Account.user_id == user_id)
        .order_by(Account.id).first()
    )
    if account is not None:
        return account
    account = Account(user_id=user_id, account_name=DEFAULT_ACCOUNT_NAME, iban_last4="0000")
    session.add(account)
    session.commit()
    session.refresh(account)
    logger.info("Cuenta por defecto creada para user_id=%d (id=%d).", user_id, account.id)
    return account


def user_accounts(session: Session, user_id: int) -> List[Account]:
    return session.query(Account).filter(Account.user_id == user_id).order_by(Account.id).all()


@dataclass
class ImportResult:
    summary: ImportSummary
    auto_categorized: int


def run_import(
    session: Session,
    user_id: int,
    preview: ImportPreview,
    account_id: int,
    decisions: Dict[int, bool],
) -> ImportResult:
    """Write the rows (clean ones always; possible duplicates only when
    decisions[row] is True; exact duplicates never) and categorize the new
    transactions with the user's rules."""
    account = session.get(Account, account_id)
    if account is None or account.user_id != user_id:
        raise LookupError("Cuenta no encontrada.")
    summary = commit_import(preview, session, user_id, account_id, decisions)
    auto_categorized = 0
    if summary.imported_ids:
        rules_result = apply_rules_and_commit(session, summary.imported_ids, user_id)
        auto_categorized = len(rules_result.assigned)
    return ImportResult(summary=summary, auto_categorized=auto_categorized)
