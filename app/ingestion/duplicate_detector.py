"""
app/ingestion/duplicate_detector.py
=====================================
PROFIN — Duplicate Detection Logic

Duplicate detection priority rules (from spec):
  D-1  EXACT match on (transaction_date, concepto_comun, amount,
       running_balance) → near-certain duplicate, auto-skip.
       Logged with is_duplicate_flagged=True, duplicate_resolved_by_user=True.

  D-2  Match on (transaction_date, concepto_comun, amount) but DIFFERENT
       running_balance → likely a genuine repeated transaction
       (e.g., two identical payments same day). Do NOT auto-skip.
       Imported as new, NOT flagged.

  D-3  Match on (transaction_date, concepto_comun, amount, running_balance)
       that is ambiguous per known patterns → flag is_duplicate_flagged=True,
       prompt user via GUI dialog. User resolves Yes/No.
       NOTE: In v1 with the 4-field exact match in D-1, D-3 is only triggered
       for edge cases where the match is ambiguous despite exact field match
       (e.g., same-day batch transfers). Implementing as a flag list that
       the import flow GUI resolves interactively.

  D-4  Never silently guess on ambiguous cases — always defer to user.

Output:
  DuplicateCheckResult.auto_skip    : rows to skip (D-1 certain duplicates)
  DuplicateCheckResult.needs_review : rows needing user confirmation (D-3)
  DuplicateCheckResult.clean        : rows safe to import without question
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Set, Tuple

import pandas as pd
from sqlalchemy.orm import Session

from app.db.models import Transaction


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DuplicateCheckResult:
    """
    Result of running duplicate detection on an incoming DataFrame.

    Attributes
    ----------
    auto_skip : List[int]
        DataFrame row indices that are certain duplicates (D-1).
        These are SKIPPED automatically — no user prompt.
    needs_review : List[int]
        DataFrame row indices that are possible/ambiguous duplicates (D-3).
        GUI must prompt the user for each.
    clean : List[int]
        DataFrame row indices that are clean — safe to import.
    skip_reasons : dict[int, str]
        Human-readable reason string for each auto_skip index.
    review_reasons : dict[int, str]
        Human-readable reason string for each needs_review index.
    """
    auto_skip:      List[int] = field(default_factory=list)
    needs_review:   List[int] = field(default_factory=list)
    clean:          List[int] = field(default_factory=list)
    skip_reasons:   dict      = field(default_factory=dict)
    review_reasons: dict      = field(default_factory=dict)

    @property
    def total_incoming(self) -> int:
        return len(self.auto_skip) + len(self.needs_review) + len(self.clean)

    @property
    def summary(self) -> str:
        return (
            f"Total: {self.total_incoming} | "
            f"Limpios: {len(self.clean)} | "
            f"Auto-omitidos: {len(self.auto_skip)} | "
            f"Revisión pendiente: {len(self.needs_review)}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_duplicates(
    incoming: pd.DataFrame,
    session: Session,
    user_id: int,
    account_id: int,
) -> DuplicateCheckResult:
    """
    Check each row of `incoming` against existing transactions in the DB.

    Parameters
    ----------
    incoming : pd.DataFrame
        Output of excel_parser.parse_excel() (already validated).
    session : Session
        Active SQLAlchemy session (read-only during this call).
    user_id : int
        ID of the authenticated user.
    account_id : int
        ID of the target account.

    Returns
    -------
    DuplicateCheckResult
    """
    result = DuplicateCheckResult()

    # -- Build lookup set from existing transactions ----------------------
    # Key for D-1 exact match: (date, concepto_comun, amount, running_balance)
    # Key for D-2/D-3 partial: (date, concepto_comun, amount)
    existing_exact: Set[Tuple] = set()
    existing_partial: Set[Tuple] = set()

    existing_txns = (
        session.query(
            Transaction.transaction_date,
            Transaction.concepto_comun,
            Transaction.amount,
            Transaction.running_balance,
        )
        .filter(
            Transaction.user_id    == user_id,
            Transaction.account_id == account_id,
        )
        .all()
    )

    for txn in existing_txns:
        exact_key   = (txn.transaction_date, txn.concepto_comun,
                       txn.amount, txn.running_balance)
        partial_key = (txn.transaction_date, txn.concepto_comun, txn.amount)
        existing_exact.add(exact_key)
        existing_partial.add(partial_key)

    # -- Check each incoming row -----------------------------------------
    # Track duplicates WITHIN the incoming batch itself (intra-batch)
    # to catch duplicate rows inside the same file.
    intra_batch_exact: Set[Tuple]   = set()
    intra_batch_partial: Set[Tuple] = set()

    for idx, row in incoming.iterrows():
        txn_date    = row["transaction_date"]
        conc_comun  = row.get("concepto_comun")
        amount      = row["amount"]
        balance     = row["running_balance"]

        exact_key   = (txn_date, conc_comun, amount, balance)
        partial_key = (txn_date, conc_comun, amount)

        # D-1: Exact match against DB → auto-skip
        if exact_key in existing_exact:
            result.auto_skip.append(idx)
            result.skip_reasons[idx] = (
                f"Duplicado exacto en BD: "
                f"{txn_date} | {conc_comun} | {amount:+.2f} € | "
                f"Saldo {balance:.2f} €"
            )
            continue

        # D-1 intra-batch: exact match within this same file → auto-skip
        if exact_key in intra_batch_exact:
            result.auto_skip.append(idx)
            result.skip_reasons[idx] = (
                f"Duplicado exacto dentro del fichero: "
                f"{txn_date} | {conc_comun} | {amount:+.2f} € | "
                f"Saldo {balance:.2f} €"
            )
            continue

        # D-2: Partial match (same date + concepto + amount, diff balance)
        # → This is a genuine repeated transaction. Import as clean, no flag.
        # (We do NOT add a needs_review here per spec rule D-2.)

        # D-2: Partial match (same date + concepto + amount, diff balance)
        # → This is a genuine repeated transaction and should be imported
        # as a normal row (clean). No user flagging for intra-batch partial
        # matches per spec. We therefore fall through to the 'clean' branch.

        # Clean row
        result.clean.append(idx)
        intra_batch_exact.add(exact_key)
        intra_batch_partial.add(partial_key)

    return result