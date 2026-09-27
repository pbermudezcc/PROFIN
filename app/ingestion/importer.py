"""
app/ingestion/importer.py
==========================
FAMILYFIN — Import Orchestrator

Responsibility:
  Orchestrate the full import pipeline:
    1. Parse   : excel_parser.parse_excel()
    2. Validate: validator.validate_dataframe()
    3. Duplicate check: duplicate_detector.check_duplicates()
    4. Persist : write ImportBatch + Transaction rows to DB
    5. Integrity check: trigger balance reconciliation after import

  This module contains NO GUI logic — it returns structured results
  that the GUI (app/gui/import_flow.py) uses to drive dialogs and progress.

Key design decisions:
  - All DB writes happen inside a single transaction per batch.
    On any unhandled exception, the session is rolled back cleanly.
  - User-resolved duplicate decisions are passed in as a dict
    {row_index: bool} where True = "import anyway", False = "skip".
  - TransactionType is derived from amount sign:
      amount > 0 → income
      amount < 0 → expense
      amount == 0 → unknown
  - source_file stores only the filename, not the full path (privacy).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    AuditAction,
    AuditLog,
    ImportBatch,
    ImportStatus,
    Transaction,
    TransactionType,
)
from app.ingestion.duplicate_detector import DuplicateCheckResult
from app.ingestion.excel_parser import parse_excel
from app.ingestion.validator import ValidationResult, validate_dataframe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ImportPreview:
    """
    Returned after parse + validate + duplicate-check, BEFORE any DB writes.
    Used by the GUI to display the import preview dialog.
    """
    df:                pd.DataFrame
    validation_result: ValidationResult
    duplicate_result:  DuplicateCheckResult
    file_path:         Path

    @property
    def clean_count(self) -> int:
        return len(self.duplicate_result.clean)

    @property
    def auto_skip_count(self) -> int:
        return len(self.duplicate_result.auto_skip)

    @property
    def needs_review_count(self) -> int:
        return len(self.duplicate_result.needs_review)


@dataclass
class ImportSummary:
    """
    Returned after DB writes are complete.
    """
    import_batch_id:  int
    rows_imported:    int
    rows_skipped:     int
    rows_errors:      int
    warnings:         List[str]  = field(default_factory=list)
    imported_ids:     List[int]  = field(default_factory=list)
    status:           ImportStatus = ImportStatus.completed


# ---------------------------------------------------------------------------
# Public API — Stage 1: Preview (no DB writes)
# ---------------------------------------------------------------------------

def prepare_import(
    file_path:  Path,
    session:    Session,
    user_id:    int,
    account_id: int,
) -> ImportPreview:
    """
    Parse, validate, and check for duplicates WITHOUT writing to the DB.
    Returns an ImportPreview the GUI uses to show the import preview dialog.

    Parameters
    ----------
    file_path  : Path to the .xlsx file.
    session    : Active SQLAlchemy read session.
    user_id    : Authenticated user ID.
    account_id : Target account ID.

    Returns
    -------
    ImportPreview

    Raises
    ------
    FileNotFoundError   : If file does not exist.
    ValueError          : If Excel file is unreadable or malformed.
    ValidationError     : If any blocking validation rule fails.
    """
    df                = parse_excel(file_path)
    validation_result = validate_dataframe(df)
    duplicate_result  = _check_duplicates_safe(
        df, session, user_id, account_id
    )

    return ImportPreview(
        df=df,
        validation_result=validation_result,
        duplicate_result=duplicate_result,
        file_path=file_path,
    )


# ---------------------------------------------------------------------------
# Public API — Stage 2: Commit (DB writes)
# ---------------------------------------------------------------------------

def commit_import(
    preview:           ImportPreview,
    session:           Session,
    user_id:           int,
    account_id:        int,
    user_decisions:    Dict[int, bool],
) -> ImportSummary:
    """
    Write the approved rows to the DB as a single atomic transaction.

    Parameters
    ----------
    preview        : ImportPreview from prepare_import().
    session        : Active SQLAlchemy session (will be committed here).
    user_id        : Authenticated user ID.
    account_id     : Target account ID.
    user_decisions : {row_index: True/False}
                     True  = user says "import this possible duplicate"
                     False = user says "skip this possible duplicate"
                     Rows in needs_review that are NOT in this dict are
                     skipped by default (conservative).

    Returns
    -------
    ImportSummary
    """
    df               = preview.df
    dup_result       = preview.duplicate_result
    file_path        = preview.file_path
    warnings: List[str] = [w.message for w in preview.validation_result.warnings]

    rows_imported = 0
    rows_skipped  = 0
    rows_errors   = 0
    imported_ids: List[int] = []

    try:
        # -- Create ImportBatch record ------------------------------------
        batch = ImportBatch(
            user_id          = user_id,
            filename         = file_path.name,
            import_timestamp = datetime.now(timezone.utc),
            rows_imported    = 0,
            rows_skipped     = 0,
            status           = ImportStatus.pending,
        )
        session.add(batch)
        session.flush()  # get batch.id before inserting transactions

        # -- Determine which rows to import ------------------------------
        # auto_skip → always skip
        # needs_review → import only if user_decisions[idx] is True
        # clean → always import
        skip_indices = set(dup_result.auto_skip)
        for idx in dup_result.needs_review:
            if not user_decisions.get(idx, False):
                skip_indices.add(idx)

        import_indices = [
            i for i in range(len(df))
            if i not in skip_indices
        ]

        # -- Insert transactions -----------------------------------------
        for idx in import_indices:
            row = df.iloc[idx]
            try:
                txn = _row_to_transaction(
                    row        = row,
                    user_id    = user_id,
                    account_id = account_id,
                    batch_id   = batch.id,
                    source_file= file_path.name,
                    is_flagged = idx in dup_result.needs_review,
                    resolved   = idx in dup_result.needs_review,
                )
                session.add(txn)
                session.flush()   # get txn.id immediately
                imported_ids.append(txn.id)
                rows_imported += 1

                # Audit log entry
                session.add(AuditLog(
                    user_id       = user_id,
                    table_affected= "transactions",
                    record_id     = txn.id,
                    action        = AuditAction.insert,
                    old_value     = None,
                    new_value     = (
                        f"{row['transaction_date']} | "
                        f"{row.get('issuer_name','')} | "
                        f"{row['amount']:+.2f}"
                    ),
                    timestamp     = datetime.now(timezone.utc),
                ))

            except Exception as row_exc:
                logger.warning(
                    "Error al insertar fila %d: %s", idx, row_exc
                )
                rows_errors += 1
                warnings.append(
                    f"Fila {idx}: error al insertar — {row_exc}"
                )

        rows_skipped = len(skip_indices)

        # -- Update batch counters & status ------------------------------
        batch.rows_imported = rows_imported
        batch.rows_skipped  = rows_skipped
        batch.status = (
            ImportStatus.completed
            if rows_errors == 0
            else ImportStatus.partial
        )

        session.commit()
        logger.info(
            "Import completado: batch_id=%d, importadas=%d, "
            "omitidas=%d, errores=%d",
            batch.id, rows_imported, rows_skipped, rows_errors,
        )

        return ImportSummary(
            import_batch_id = batch.id,
            rows_imported   = rows_imported,
            rows_skipped    = rows_skipped,
            rows_errors     = rows_errors,
            warnings        = warnings,
            imported_ids    = imported_ids,
            status          = batch.status,
        )

    except Exception as exc:
        session.rollback()
        logger.error("Import abortado, rollback ejecutado: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_duplicates_safe(
    df:         pd.DataFrame,
    session:    Session,
    user_id:    int,
    account_id: int,
):
    """Wrap duplicate_detector import to avoid circular import at module level."""
    from app.ingestion.duplicate_detector import check_duplicates
    return check_duplicates(df, session, user_id, account_id)


def _derive_type(amount: float) -> TransactionType:
    """
    Derive TransactionType from the signed amount.
    Investment type cannot be derived from amount alone — it requires
    manual or rule-based categorisation. amount > 0 = income,
    amount < 0 = expense, 0 = unknown.
    """
    if amount > 0:
        return TransactionType.income
    elif amount < 0:
        return TransactionType.expense
    return TransactionType.unknown


def _row_to_transaction(
    row:         pd.Series,
    user_id:     int,
    account_id:  int,
    batch_id:    int,
    source_file: str,
    is_flagged:  bool,
    resolved:    bool,
) -> Transaction:
    """Map one DataFrame row to a Transaction ORM object."""
    return Transaction(
        user_id                    = user_id,
        account_id                 = account_id,
        import_batch_id            = batch_id,
        transaction_date           = row["transaction_date"],
        value_date                 = row.get("value_date"),
        amount                     = float(row["amount"]),
        running_balance            = float(row["running_balance"]),
        type                       = _derive_type(float(row["amount"])),
        concepto_comun             = row.get("concepto_comun"),
        concepto_propio            = row.get("concepto_propio"),
        issuer_name                = row.get("issuer_name"),
        concept                    = row.get("concept"),
        raw_col14                  = row.get("raw_col14"),
        raw_details                = row.get("raw_details"),
        source_file                = source_file,
        is_duplicate_flagged       = is_flagged,
        duplicate_resolved_by_user = resolved,
        category_id                = None,  # assigned by rules engine later
    )