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
  - One import may hold several statements of the same account. Each file
    is parsed and validated on its own (errors name the file); the accepted
    files are merged chronologically and checked for duplicates together,
    so overlapping exports never import a movement twice. A file that cannot
    be used is reported as rejected; the rest still import. The commit is
    all-or-nothing, with one ImportBatch per file.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd
from sqlalchemy import func
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
from app.ingestion.validator import (
    ValidationError,
    ValidationWarning,
    validate_dataframe,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class StatementFile:
    """One accepted statement of an import (its rows: `source_file` == name)."""
    name:     str
    path:     Path
    iban:     str
    rows:     int
    first:    date
    last:     date
    warnings: List[ValidationWarning] = field(default_factory=list)


@dataclass
class RejectedFile:
    """A selected file that is left out of the import, and why."""
    name:   str
    reason: str


class NoUsableStatement(ValidationError):
    """No selected file can be imported; `rejected` says why, file by file."""

    def __init__(self, rejected: List[RejectedFile]) -> None:
        self.rejected = rejected
        super().__init__(
            rejected[0].reason if len(rejected) == 1 else
            "Ningún fichero se puede importar:\n"
            + "\n".join(f"• {r.name}: {r.reason}" for r in rejected)
        )


@dataclass
class FileStats:
    clean:        int = 0
    needs_review: int = 0
    auto_skip:    int = 0


@dataclass
class ImportPreview:
    """
    Returned after parse + validate + duplicate-check, BEFORE any DB writes.
    Used by the GUI to display the import preview dialog.

    `df` holds the rows of every accepted file, in chronological file order,
    with a `source_file` column naming the file each row came from.
    """
    df:               pd.DataFrame
    files:            List[StatementFile]
    duplicate_result: DuplicateCheckResult
    rejected:         List[RejectedFile] = field(default_factory=list)

    @property
    def iban(self) -> str:
        return self.files[0].iban if self.files else ""

    @property
    def warnings(self) -> List[ValidationWarning]:
        """Validation warnings of every file (named when there are several)."""
        many = len(self.files) > 1
        return [
            ValidationWarning(w.rule, f"«{f.name}»: {w.message}" if many else w.message)
            for f in self.files for w in f.warnings
        ]

    def file_stats(self) -> Dict[str, FileStats]:
        """Clean / review / skip counts per file name, in file order."""
        stats = {f.name: FileStats() for f in self.files}
        sources = self.df["source_file"]
        dup = self.duplicate_result
        for attr, indices in (("clean", dup.clean), ("needs_review", dup.needs_review),
                              ("auto_skip", dup.auto_skip)):
            for idx in indices:
                entry = stats[sources.iloc[idx]]
                setattr(entry, attr, getattr(entry, attr) + 1)
        return stats

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
    Returned after DB writes are complete (totals over every file).
    """
    batch_ids:        List[int]
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
    file_paths: Sequence[Path],
    session:    Session,
    user_id:    int,
    account_id: int,
) -> ImportPreview:
    """
    Parse, validate, and check for duplicates WITHOUT writing to the DB.
    Returns an ImportPreview the GUI uses to show the import preview dialog.

    Parameters
    ----------
    file_paths : One or more CaixaBank .xls/.xlsx statements of one account.
    session    : Active SQLAlchemy read session.
    user_id    : Authenticated user ID.
    account_id : Target account ID.

    Returns
    -------
    ImportPreview — the files that cannot be used are listed in `rejected`.

    Raises
    ------
    NoUsableStatement (a ValidationError) : If no file can be imported.
    """
    accepted: List[Tuple[StatementFile, pd.DataFrame]] = []
    rejected: List[RejectedFile] = []
    digests: Dict[str, str] = {}
    taken: Set[str] = set()

    for raw_path in file_paths:
        path = Path(raw_path)
        name = _unique_name(path.name, taken)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            rejected.append(RejectedFile(name, f"No se puede leer el fichero: {exc}"))
            continue
        if digest in digests:
            rejected.append(RejectedFile(name, f"Es el mismo fichero que «{digests[digest]}»."))
            continue
        try:
            df = parse_excel(path)
            validation = validate_dataframe(df)
        except (ValidationError, ValueError) as exc:
            rejected.append(RejectedFile(name, str(exc)))
            continue
        digests[digest] = name
        accepted.append((StatementFile(
            name=name, path=path, iban=validation.iban, rows=len(df),
            first=df["transaction_date"].min(), last=df["transaction_date"].max(),
            warnings=validation.warnings,
        ), df))

    accepted.sort(key=lambda item: (item[0].first, item[0].last))
    accepted = _same_account(accepted, rejected, session, account_id)

    if not accepted:
        raise NoUsableStatement(rejected)

    df = pd.concat(
        [frame.assign(source_file=f.name) for f, frame in accepted],
        ignore_index=True,
    )
    duplicate_result = _check_duplicates_safe(df, session, user_id, account_id)

    return ImportPreview(
        df=df,
        files=[f for f, _ in accepted],
        duplicate_result=duplicate_result,
        rejected=rejected,
    )


# ---------------------------------------------------------------------------
# Public API — Stage 2: Commit (DB writes)
# ---------------------------------------------------------------------------

def _chronological_indices(df: pd.DataFrame) -> List[int]:
    """Row indices of `df` oldest first: statements in their (chronological)
    order and, inside each, the bank's order of time. CaixaBank lists a
    statement newest first; one already listed oldest first is kept as is."""
    order: List[int] = []
    for _, group in df.groupby("source_file", sort=False):
        indices = list(group.index)
        dates = list(group["transaction_date"])
        ascending = len(dates) > 1 and dates[0] < dates[-1]
        order.extend(indices if ascending else reversed(indices))
    return order


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
                     True  = user says "import this row"
                     False = user says "skip this row"
                     Rows in needs_review that are NOT in this dict are
                     skipped by default (conservative); clean rows that
                     are NOT in it are imported.

    Returns
    -------
    ImportSummary
    """
    df               = preview.df
    dup_result       = preview.duplicate_result
    warnings: List[str] = [w.message for w in preview.warnings]

    rows_imported = 0
    rows_skipped  = 0
    rows_errors   = 0
    imported_ids: List[int] = []

    try:
        # -- One ImportBatch record per file -------------------------------
        now = datetime.now(timezone.utc)
        batches: Dict[str, ImportBatch] = {}
        for statement in preview.files:
            batches[statement.name] = ImportBatch(
                user_id          = user_id,
                filename         = statement.name,
                import_timestamp = now,
                rows_imported    = 0,
                rows_skipped     = 0,
                status           = ImportStatus.pending,
            )
            session.add(batches[statement.name])
        session.flush()  # get the batch ids before inserting transactions
        errors_by_file: Counter = Counter()

        # -- Determine which rows to import ------------------------------
        # auto_skip → always skip
        # needs_review → import only if user_decisions[idx] is True
        # clean → import unless the user unticked it (user_decisions[idx] False)
        skip_indices = set(dup_result.auto_skip)
        for idx in dup_result.needs_review:
            if not user_decisions.get(idx, False):
                skip_indices.add(idx)
        for idx in dup_result.clean:
            if user_decisions.get(idx, True) is False:
                skip_indices.add(idx)

        import_indices = [
            i for i in _chronological_indices(df)
            if i not in skip_indices
        ]
        # Same-day movements are ordered by bank_order (higher = later),
        # numbered after this user's latest one.
        last_order = (
            session.query(func.max(Transaction.bank_order))
            .filter(Transaction.user_id == user_id)
            .scalar()
        ) or 0

        # -- Insert transactions -----------------------------------------
        for idx in import_indices:
            row = df.iloc[idx]
            source = row["source_file"]
            batch = batches[source]
            try:
                txn = _row_to_transaction(
                    row        = row,
                    user_id    = user_id,
                    account_id = account_id,
                    batch_id   = batch.id,
                    source_file= source,
                    is_flagged = idx in dup_result.needs_review,
                    resolved   = idx in dup_result.needs_review,
                )
                last_order += 1
                txn.bank_order = last_order
                session.add(txn)
                session.flush()   # get txn.id immediately
                imported_ids.append(txn.id)
                rows_imported += 1
                batch.rows_imported += 1

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
                errors_by_file[source] += 1
                warnings.append(
                    f"«{source}» fila {idx}: error al insertar — {row_exc}"
                )

        rows_skipped = len(skip_indices)

        # -- Update batch counters & status ------------------------------
        for idx in skip_indices:
            batches[df["source_file"].iloc[idx]].rows_skipped += 1
        for name, batch in batches.items():
            batch.status = (
                ImportStatus.completed
                if errors_by_file[name] == 0
                else ImportStatus.partial
            )

        session.commit()
        batch_ids = [b.id for b in batches.values()]
        logger.info(
            "Import completado: batch_ids=%s, importadas=%d, "
            "omitidas=%d, errores=%d",
            batch_ids, rows_imported, rows_skipped, rows_errors,
        )

        return ImportSummary(
            batch_ids       = batch_ids,
            rows_imported   = rows_imported,
            rows_skipped    = rows_skipped,
            rows_errors     = rows_errors,
            warnings        = warnings,
            imported_ids    = imported_ids,
            status          = (
                ImportStatus.completed if rows_errors == 0
                else ImportStatus.partial
            ),
        )

    except Exception as exc:
        session.rollback()
        logger.error("Import abortado, rollback ejecutado: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _unique_name(name: str, taken: Set[str]) -> str:
    """CaixaBank names exports alike: keep the names in one import distinct."""
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate, n = name, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{stem} ({n}){suffix}"
    taken.add(candidate.lower())
    return candidate


def _iban_tail(iban: Optional[str]) -> str:
    return re.sub(r"\D", "", iban or "")[-4:]


def _same_account(
    accepted:   List[Tuple[StatementFile, pd.DataFrame]],
    rejected:   List[RejectedFile],
    session:    Session,
    account_id: int,
) -> List[Tuple[StatementFile, pd.DataFrame]]:
    """Keep only the files of one account: the target account's IBAN when it
    is known, otherwise the IBAN most of the files share."""
    account = session.get(Account, account_id)
    known = (account.iban_last4 or "") if account is not None else ""
    if known in ("", "0000"):
        tails = Counter(_iban_tail(f.iban) for f, _ in accepted)
        known = tails.most_common(1)[0][0] if tails else ""
    kept = []
    for statement, frame in accepted:
        tail = _iban_tail(statement.iban)
        if tail == known:
            kept.append((statement, frame))
        else:
            rejected.append(RejectedFile(
                statement.name,
                f"Es de otra cuenta (··{tail}); esta importación va a la cuenta ··{known}.",
            ))
    return kept


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