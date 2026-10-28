"""
tests/test_importer.py
=======================
commit_import(): persisted fields, audit trail, and duplicate decisions.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.auth_service import create_user
from app.db.models import Account, AuditLog, Base, Transaction
from app.ingestion.duplicate_detector import DuplicateCheckResult
from app.ingestion.importer import ImportPreview, StatementFile, commit_import


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _preview(rows: list[dict], duplicates: DuplicateCheckResult) -> ImportPreview:
    defaults = {
        "transaction_date": date(2026, 9, 1),
        "value_date":       date(2026, 9, 1),
        "amount":           -20.0,
        "running_balance":  500.0,
        "concepto_comun":   "044",
        "concepto_propio":  "000",
        "issuer_name":      "MERCADONA",
        "concept":          "MERCADONA | TARJETA",
        "raw_col14":        "MERCADONA",
        "raw_details":      "TARJETA   1234",
        "currency":         "EUR",
        "iban_raw":         "ES00",
        "source_file":      "movimientos.xls",
    }
    df = pd.DataFrame([{**defaults, **row} for row in rows])
    statement = StatementFile(
        name="movimientos.xls", path=Path("C:/banco/movimientos.xls"), iban="ES00",
        rows=len(df), first=date(2026, 9, 1), last=date(2026, 9, 1),
    )
    return ImportPreview(df=df, files=[statement], duplicate_result=duplicates)


def test_commit_import_persists_raw_details_and_audit_record_id(db_session):
    user = create_user(db_session, "ana", "clave-secreta-1")
    account = Account(user_id=user.id, account_name="Cuenta")
    db_session.add(account)
    db_session.commit()

    preview = _preview([{}], DuplicateCheckResult(clean=[0]))
    summary = commit_import(preview, db_session, user.id, account.id, {})

    txn = db_session.get(Transaction, summary.imported_ids[0])
    assert txn.raw_details == "TARJETA   1234"
    assert txn.source_file == "movimientos.xls"
    audit = db_session.query(AuditLog).one()
    assert audit.record_id == txn.id


def test_commit_import_skips_unresolved_review_rows(db_session):
    user = create_user(db_session, "ana", "clave-secreta-1")
    account = Account(user_id=user.id, account_name="Cuenta")
    db_session.add(account)
    db_session.commit()

    duplicates = DuplicateCheckResult(clean=[0], needs_review=[1, 2])
    preview = _preview([{}, {"amount": 0.0}, {"amount": 0.0}], duplicates)
    summary = commit_import(preview, db_session, user.id, account.id, {2: True})

    assert summary.rows_imported == 2
    assert summary.rows_skipped == 1
    flagged = db_session.get(Transaction, summary.imported_ids[1])
    assert flagged.is_duplicate_flagged is True
