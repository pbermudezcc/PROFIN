"""
app/ingestion/duplicate_detector.py
=====================================
FAMILYFIN — Duplicate Detection Logic

Duplicate detection priority rules (from spec):
  D-1  EXACT match on (transaction_date, concepto_comun, amount,
       running_balance) → near-certain duplicate, auto-skip.
       Logged with is_duplicate_flagged=True, duplicate_resolved_by_user=True.

  D-2  Match on (transaction_date, concepto_comun, amount) but DIFFERENT
       running_balance → likely a genuine repeated transaction
       (e.g., two identical payments same day). Do NOT auto-skip.
       Imported as new, NOT flagged.

  D-3  Exact 4-field match on a ZERO-amount row → ambiguous. A 0,00 €
       movement does not change the running balance, so the balance cannot
       tell a re-imported row from a genuine second one. Such rows go to
       needs_review (is_duplicate_flagged=True if imported) and the import
       GUI asks the user; unresolved rows are skipped (conservative).

  D-4  Never silently guess on ambiguous cases — always defer to user.

  D-5  One import may hold several overlapping exports (column
       `source_file`). A row another file of the same import already holds
       as many times is that same movement → auto-skip.

Output:
  DuplicateCheckResult.auto_skip    : rows to skip (D-1 certain duplicates)
  DuplicateCheckResult.needs_review : rows needing user confirmation (D-3)
  DuplicateCheckResult.clean        : rows safe to import without question
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Set, Tuple

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
    existing_exact: Set[Tuple] = set()

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
        existing_exact.add((txn.transaction_date, txn.concepto_comun,
                            txn.amount, txn.running_balance))

    # -- Check each incoming row -----------------------------------------
    # Track the rows already seen in THIS import, per source file. A batch can
    # hold several overlapping CaixaBank exports of the same account: a row
    # that another file of the batch already holds the same number of times
    # is the same movement exported twice, not a new one. Inside one file, an
    # exact repeat follows the D-1 / D-3 rules as before.
    seen: Dict[Tuple, Dict[object, int]] = {}
    has_source = "source_file" in incoming.columns

    for idx, row in incoming.iterrows():
        txn_date    = row["transaction_date"]
        conc_comun  = row.get("concepto_comun")
        amount      = row["amount"]
        balance     = row["running_balance"]
        source      = row["source_file"] if has_source else None

        exact_key   = (txn_date, conc_comun, amount, balance)
        counts      = seen.setdefault(exact_key, {})
        in_this     = counts.get(source, 0)
        other       = max(
            ((n, src) for src, n in counts.items() if src != source),
            default=(0, None), key=lambda item: item[0],
        )
        counts[source] = in_this + 1

        # Same movement already present in another file of this import
        if in_this < other[0]:
            result.auto_skip.append(idx)
            result.skip_reasons[idx] = (
                f"Duplicado exacto en el extracto «{other[1]}»: "
                f"{txn_date} | {conc_comun} | {amount:+.2f} € | "
                f"Saldo {balance:.2f} €"
            )
            continue

        # D-3: exact match on a zero-amount row → ambiguous, ask the user
        if amount == 0 and (exact_key in existing_exact or in_this):
            result.needs_review.append(idx)
            where = "en BD" if exact_key in existing_exact else "dentro del fichero"
            result.review_reasons[idx] = (
                f"Posible duplicado {where} con importe 0,00 € "
                f"(el saldo no permite distinguirlo): "
                f"{txn_date} | {conc_comun} | Saldo {balance:.2f} €"
            )
            continue

        # D-1: Exact match against DB → auto-skip
        if exact_key in existing_exact:
            result.auto_skip.append(idx)
            result.skip_reasons[idx] = (
                f"Duplicado exacto en BD: "
                f"{txn_date} | {conc_comun} | {amount:+.2f} € | "
                f"Saldo {balance:.2f} €"
            )
            continue

        # D-1 intra-file: exact match within this same file → auto-skip
        if in_this:
            result.auto_skip.append(idx)
            result.skip_reasons[idx] = (
                f"Duplicado exacto dentro del fichero: "
                f"{txn_date} | {conc_comun} | {amount:+.2f} € | "
                f"Saldo {balance:.2f} €"
            )
            continue

        # D-2: Partial match (same date + concepto + amount, diff balance)
        # → a genuine repeated transaction: imported as a normal (clean) row.
        result.clean.append(idx)

    return result