"""
tests/test_import_pipeline.py
==============================
End-to-end import of a real .xlsx laid out like a CaixaBank export:
parse → validate → duplicate check → commit → auto-categorize, then the
same file imported again must not create a single duplicate.
"""
from __future__ import annotations

from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.auth_service import create_user
from app.categorization.category_manager import (
    seed_default_categories,
    seed_default_rules,
)
from app.categorization.rules_engine import apply_rules_and_commit
from app.db.models import (
    CATEGORY_SOURCE_RULE, Account, AuditLog, Category, ImportBatch,
    ImportStatus, Transaction, TransactionType,
)
from app.db.models import Base
from app.ingestion.importer import commit_import, prepare_import
from app.ingestion.validator import ValidationError

IBAN = "ES12 2100 0000 0000 0000 0000"


def _row(fecha, ingreso=None, gasto=None, saldo_pos=None, saldo_neg=None,
         comun="044", issuer="", details=(), iban=IBAN):
    row = [None] * 24
    row[1] = iban
    row[3] = "EUR"
    row[4] = fecha
    row[5] = fecha
    row[6] = ingreso
    row[7] = gasto
    row[8] = saldo_pos
    row[9] = saldo_neg
    row[10] = comun
    row[11] = "000"
    row[12] = "000000000000"
    row[14] = issuer
    for offset, text in enumerate(details):
        row[15 + offset] = text
    return row


def _write_export(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append([None] * 24)
    ws.append([None, "MOVIMIENTOS DESDE 01/09/2026 HASTA 30/09/2026"] + [None] * 22)
    ws.append([None] * 24)
    # The real export labels every column, so the sheet is 24 columns wide
    # even when the trailing detail cells of every movement are empty.
    header = [f"Col {i}" for i in range(24)]
    header[4], header[5], header[6], header[7] = "F. Operación", "F. Valor", "Ingreso (+)", "Gasto (-)"
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


EXPORT_ROWS = [
    _row("01/09/2026", gasto=45.3, saldo_pos=1954.7,
         issuer="Fecha de operación: 01-09-2026   IBERDROLA CLIENTES", details=("RECIBO LUZ",)),
    _row("02/09/2026", ingreso=1500.0, saldo_pos=3454.7, comun="012",
         issuer="MANPOWER TEAM E.T.T.", details=("NOMINA", "SEPTIEMBRE")),
    _row("03/09/2026", gasto=12.35, saldo_pos=3442.35, issuer="PANADERIA LOLA"),
    _row("04/09/2026", gasto=3500.0, saldo_neg=-57.65, issuer="TRANSFERENCIA A AHORRO"),
    _row("05/09/2026", gasto=0.0, saldo_neg=-57.65, comun="099", issuer="AJUSTE"),
]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def family(session):
    user = create_user(session, "maria", "clave-secreta-1")
    account = Account(user_id=user.id, account_name="CaixaBank Principal")
    session.add(account)
    session.commit()
    seed_default_categories(session, user.id)
    seed_default_rules(session, user.id)
    return user.id, account.id


def _import(paths, session, user_id, account_id, decisions=None):
    """Mirror ImportView: clean rows are imported, review rows only if approved."""
    paths = paths if isinstance(paths, list) else [paths]
    preview = prepare_import(paths, session, user_id, account_id)
    chosen = {idx: True for idx in preview.duplicate_result.clean}
    chosen.update(decisions or {})
    summary = commit_import(preview, session, user_id, account_id, chosen)
    if summary.imported_ids:
        apply_rules_and_commit(session, summary.imported_ids, user_id)
    return preview, summary


def test_first_import_persists_categorizes_and_audits(tmp_path, session, family):
    user_id, account_id = family
    path = tmp_path / "movimientos.xlsx"
    _write_export(path, EXPORT_ROWS)

    preview, summary = _import(path, session, user_id, account_id)

    assert preview.iban == IBAN
    assert [w.rule for w in preview.warnings] == ["V-08"]
    assert summary.rows_imported == 5 and summary.rows_skipped == 0
    assert summary.status == ImportStatus.completed

    txns = {t.issuer_name: t for t in session.query(Transaction).all()}
    luz = txns["IBERDROLA CLIENTES"]
    assert luz.amount == -45.3 and luz.type == TransactionType.expense
    assert luz.transaction_date == date(2026, 9, 1)
    assert luz.raw_details == "RECIBO LUZ"
    assert luz.source_file == "movimientos.xlsx"
    assert session.get(Category, luz.category_id).name == "Luz"
    assert luz.category_source == CATEGORY_SOURCE_RULE

    nomina = txns["MANPOWER TEAM E.T.T."]
    assert nomina.amount == 1500.0 and nomina.type == TransactionType.income
    category = session.get(Category, nomina.category_id)
    assert (category.name, category.parent.name) == ("Nomina Maria", "INGRESO")

    assert txns["TRANSFERENCIA A AHORRO"].running_balance == -57.65
    assert txns["PANADERIA LOLA"].category_id is None
    assert txns["AJUSTE"].type == TransactionType.unknown

    audit_ids = {a.record_id for a in session.query(AuditLog).all()}
    assert audit_ids == {t.id for t in txns.values()}


def test_reimporting_the_same_file_creates_no_duplicates(tmp_path, session, family):
    user_id, account_id = family
    path = tmp_path / "movimientos.xlsx"
    _write_export(path, EXPORT_ROWS)
    _import(path, session, user_id, account_id)

    preview, summary = _import(path, session, user_id, account_id)

    dup = preview.duplicate_result
    assert sorted(dup.auto_skip) == [0, 1, 2, 3]
    assert dup.needs_review == [4]          # 0,00 € row: balance can't tell
    assert summary.rows_imported == 0
    assert session.query(Transaction).count() == 5
    assert session.query(ImportBatch).count() == 2


def test_overlapping_export_imports_only_new_rows(tmp_path, session, family):
    user_id, account_id = family
    first, second = tmp_path / "agosto.xlsx", tmp_path / "septiembre.xlsx"
    _write_export(first, EXPORT_ROWS[:3])
    _write_export(second, EXPORT_ROWS[1:4])
    _import(first, session, user_id, account_id)

    _, summary = _import(second, session, user_id, account_id)

    assert summary.rows_imported == 1
    assert summary.rows_skipped == 2
    assert session.query(Transaction).count() == 4


def test_unticked_clean_row_is_not_imported(tmp_path, session, family):
    # ImportView lets the user untick any row; a clean row set to False
    # used to be imported anyway.
    user_id, account_id = family
    path = tmp_path / "movimientos.xlsx"
    _write_export(path, EXPORT_ROWS)

    _, summary = _import(path, session, user_id, account_id, decisions={2: False})

    assert summary.rows_imported == 4 and summary.rows_skipped == 1
    assert "PANADERIA LOLA" not in {t.issuer_name for t in session.query(Transaction).all()}
    assert session.query(ImportBatch).one().rows_skipped == 1


def test_clean_rows_missing_from_the_decisions_are_imported(tmp_path, session, family):
    # The web sends decisions for the rows under review only.
    user_id, account_id = family
    path = tmp_path / "movimientos.xlsx"
    _write_export(path, EXPORT_ROWS)
    preview = prepare_import([path], session, user_id, account_id)

    summary = commit_import(preview, session, user_id, account_id, {})

    assert summary.rows_imported == 5


def test_file_with_two_accounts_is_rejected_before_any_write(tmp_path, session, family):
    user_id, account_id = family
    path = tmp_path / "mezcla.xlsx"
    _write_export(path, [EXPORT_ROWS[0], _row("02/09/2026", gasto=1.0, saldo_pos=1.0,
                                             issuer="X", iban="ES99 0000")])
    with pytest.raises(ValidationError):
        prepare_import([path], session, user_id, account_id)
    assert session.query(Transaction).count() == 0


# ---------------------------------------------------------------------------
# Several statements in one import
# ---------------------------------------------------------------------------

def test_overlapping_statements_in_one_import_store_each_movement_once(tmp_path, session, family):
    user_id, account_id = family
    september, overlap = tmp_path / "septiembre.xlsx", tmp_path / "semana.xlsx"
    _write_export(september, EXPORT_ROWS[:4])
    _write_export(overlap, EXPORT_ROWS[2:])           # rows 2-3 also in september

    preview, summary = _import([overlap, september], session, user_id, account_id)

    assert [f.name for f in preview.files] == ["septiembre.xlsx", "semana.xlsx"]   # chronological
    stats = preview.file_stats()
    assert (stats["septiembre.xlsx"].clean, stats["septiembre.xlsx"].auto_skip) == (4, 0)
    assert (stats["semana.xlsx"].clean, stats["semana.xlsx"].auto_skip) == (1, 2)
    assert "septiembre.xlsx" in preview.duplicate_result.skip_reasons[5]
    assert summary.rows_imported == 5 and summary.rows_skipped == 2
    assert len(summary.batch_ids) == 2

    batches = {b.filename: b for b in session.query(ImportBatch).all()}
    assert (batches["septiembre.xlsx"].rows_imported, batches["septiembre.xlsx"].rows_skipped) == (4, 0)
    assert (batches["semana.xlsx"].rows_imported, batches["semana.xlsx"].rows_skipped) == (1, 2)
    ajuste = session.query(Transaction).filter_by(issuer_name="AJUSTE").one()
    assert ajuste.source_file == "semana.xlsx" and ajuste.import_batch_id == batches["semana.xlsx"].id
    assert session.query(Transaction).count() == 5


def test_zero_amount_row_in_two_overlapping_statements_is_not_asked_twice(tmp_path, session, family):
    user_id, account_id = family
    first, second = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    _write_export(first, EXPORT_ROWS[3:])
    _write_export(second, [*EXPORT_ROWS[3:], _row("06/09/2026", gasto=1.0, saldo_neg=-58.65, issuer="X")])

    preview = prepare_import([first, second], session, user_id, account_id)

    dup = preview.duplicate_result
    assert dup.needs_review == [] and sorted(dup.auto_skip) == [2, 3]
    assert sorted(dup.clean) == [0, 1, 4]


def test_the_same_statement_twice_is_reported_and_imported_once(tmp_path, session, family):
    user_id, account_id = family
    path = tmp_path / "movimientos.xlsx"
    _write_export(path, EXPORT_ROWS)
    copy = tmp_path / "copia" / "movimientos.xlsx"
    copy.parent.mkdir()
    copy.write_bytes(path.read_bytes())

    preview, summary = _import([path, copy], session, user_id, account_id)

    assert [r.name for r in preview.rejected] == ["movimientos (2).xlsx"]
    assert "mismo fichero" in preview.rejected[0].reason
    assert summary.rows_imported == 5 and len(summary.batch_ids) == 1


def test_statement_of_another_account_is_left_out(tmp_path, session, family):
    user_id, account_id = family
    ours, other = tmp_path / "nuestra.xlsx", tmp_path / "otra.xlsx"
    _write_export(ours, EXPORT_ROWS[:3])
    _write_export(other, [_row("02/09/2026", gasto=1.0, saldo_pos=1.0, issuer="X", iban="ES99 9999 1234")])

    preview, summary = _import([ours, other, tmp_path / "falta.xlsx"], session, user_id, account_id)

    reasons = {r.name: r.reason for r in preview.rejected}
    assert set(reasons) == {"otra.xlsx", "falta.xlsx"}
    assert "otra cuenta (··1234)" in reasons["otra.xlsx"]
    assert summary.rows_imported == 3


def test_no_usable_statement_raises_with_every_reason(tmp_path, session, family):
    user_id, account_id = family
    bad = tmp_path / "roto.xlsx"
    bad.write_bytes(b"PK\x03\x04 not really a workbook")
    with pytest.raises(ValidationError) as exc:
        prepare_import([bad, tmp_path / "falta.xlsx"], session, user_id, account_id)
    assert "roto.xlsx" in str(exc.value) and "falta.xlsx" in str(exc.value)


def test_a_failed_commit_writes_nothing_from_any_statement(tmp_path, session, family, monkeypatch):
    user_id, account_id = family
    first, second = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    _write_export(first, EXPORT_ROWS[:2])
    _write_export(second, EXPORT_ROWS[2:4])
    preview = prepare_import([first, second], session, user_id, account_id)

    def boom():
        raise RuntimeError("disco lleno")
    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        commit_import(preview, session, user_id, account_id, {})
    monkeypatch.undo()

    assert session.query(Transaction).count() == 0
    assert session.query(ImportBatch).count() == 0


def test_run_import_learns_the_account_iban(tmp_path, session, family):
    from app.ingestion.import_service import ensure_default_account, run_import

    user_id, _ = family
    session.query(Account).delete()
    session.commit()
    account = ensure_default_account(session, user_id)
    assert account.iban_last4 == "0000"
    path = tmp_path / "movimientos.xlsx"
    _write_export(path, [_row("01/09/2026", gasto=5.0, saldo_pos=95.0, issuer="BAR",
                              iban="ES12 2100 0000 0000 0000 7363")])

    preview = prepare_import([path], session, user_id, account.id)
    run_import(session, user_id, preview, account.id, {})

    session.expire_all()
    assert session.get(Account, account.id).iban_last4 == "7363"
