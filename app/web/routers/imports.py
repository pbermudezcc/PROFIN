"""Importar: CaixaBank statement upload → preview with duplicates → confirm.

The file is checked (size, extension, real Excel signature), parsed on the
PC in a private temp folder, and the preview is kept server-side under a
random token bound to the user and the browser session. Nothing is written
until the user confirms; the temp folder is deleted on confirm, cancel or
expiry.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Account, AuditAction, ImportBatch, Transaction
from app.web.deps import DB, USER, WebUser, notify_change
from app.web.render import audit, parse_int, render
from app.web.uploads import remove_directory

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FILE_BYTES = 5 * 1024 * 1024
EXTENSIONS = {".xls": b"\xd0\xcf\x11\xe0", ".xlsx": b"PK\x03\x04"}   # OLE2 / ZIP signatures
UNAVAILABLE = ("La importación no está disponible en este equipo: Windows ha bloqueado una librería "
               "necesaria (pandas). Puedes importar el extracto desde la aplicación de escritorio más tarde.")

# The ingestion pipeline (and pandas, whose native DLLs Windows Smart App
# Control can block) is imported inside the handlers, so a blocked pandas
# only disables importing instead of keeping the whole web server down.


class UploadRejected(ValueError):
    pass


def _safe_name(filename: Optional[str]) -> str:
    name = Path(filename or "").name                      # drop any path part
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^\w .()-]", "_", stem).strip(" .")[:80] or "extracto"
    return f"{stem}{ext}"


def _read_upload(upload: UploadFile) -> tuple[str, bytes]:
    name = _safe_name(upload.filename)
    ext = Path(name).suffix
    if ext not in EXTENSIONS:
        raise UploadRejected("El fichero debe ser un extracto de CaixaBank en Excel (.xls o .xlsx).")
    data = upload.file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise UploadRejected("El fichero es demasiado grande (máximo 5 MB).")
    if not data:
        raise UploadRejected("El fichero está vacío.")
    if not data.startswith(EXTENSIONS[ext]):
        raise UploadRejected("El fichero no es un Excel válido.")
    return name, data


def _store(request: Request):
    return request.app.state.imports


def _page(request: Request, db: Session, user: WebUser, error: Optional[str] = None,
          account_id: Optional[int] = None) -> Response:
    batches = (
        db.query(ImportBatch).filter(ImportBatch.user_id == user.id)
        .order_by(ImportBatch.import_timestamp.desc()).limit(5).all()
    )
    return render(request, "importar.html", {
        "accounts": db.query(Account).filter(Account.user_id == user.id).order_by(Account.id).all(),
        "account_id": account_id, "error": error,
        "batches": batches, "max_mb": MAX_FILE_BYTES // (1024 * 1024),
    }, user=user, active="importar")


@router.get("/importar")
def import_page(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    return _page(request, db, user)


@router.post("/importar")
def import_upload(
    request: Request,
    fichero: UploadFile = File(...),
    cuenta: str = Form(""),
    user: WebUser = USER,
    db: Session = DB,
) -> Response:
    account_id = parse_int(cuenta)
    try:
        name, data = _read_upload(fichero)
    except UploadRejected as exc:
        return _page(request, db, user, error=str(exc), account_id=account_id)
    try:
        from app.ingestion.import_service import ensure_default_account
        from app.ingestion.importer import prepare_import
        from app.ingestion.validator import ValidationError
    except ImportError:
        logger.exception("Web: no se pudo cargar el módulo de importación")
        return _page(request, db, user, error=UNAVAILABLE, account_id=account_id)

    if account_id is None:
        account = ensure_default_account(db, user.id)
    else:
        account = db.get(Account, account_id)
        if account is None or account.user_id != user.id:
            raise HTTPException(status_code=404)

    store = _store(request)
    directory = store.new_directory()
    path = directory / name
    path.write_bytes(data)
    try:
        # Sync endpoint: FastAPI runs it in a worker thread, so parsing never
        # blocks the server's event loop.
        preview = prepare_import(path, db, user.id, account.id)
    except (ValidationError, ValueError) as exc:
        remove_directory(directory)
        return _page(request, db, user, error=f"No se pudo leer el extracto: {exc}", account_id=account.id)
    except Exception:
        remove_directory(directory)
        logger.exception("Web: error inesperado al leer un extracto")
        return _page(request, db, user, error="No se pudo leer el extracto. ¿Es una exportación de CaixaBank?",
                     account_id=account.id)
    token = store.add(user.id, user.session.sid, account.id, name, directory, preview)
    return RedirectResponse(f"/importar/{token}", status_code=303)


def _pending(request: Request, user: WebUser, token: str):
    item = _store(request).get(token, user.id, user.session.sid)
    if item is None:
        raise HTTPException(status_code=404)
    return item


def _num(value) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def _rows(preview, indices: List[int], status: str) -> list:
    df = preview.df
    dup = preview.duplicate_result
    reasons = {**dup.skip_reasons, **dup.review_reasons}
    out = []
    for idx in indices:
        row = df.iloc[idx]
        day = row.get("transaction_date")
        out.append({
            "idx": idx,
            "date": day if isinstance(day, date) else None,
            "issuer": (row.get("issuer_name") or "") if isinstance(row.get("issuer_name"), str) else "",
            "concept": (row.get("concept") or "") if isinstance(row.get("concept"), str) else "",
            "amount": _num(row.get("amount")) or 0.0,
            "balance": _num(row.get("running_balance")),
            "status": status,
            "reason": reasons.get(idx, ""),
        })
    return out


@router.get("/importar/{token}")
def import_preview(request: Request, token: str, user: WebUser = USER, db: Session = DB) -> Response:
    item = _pending(request, user, token)
    preview = item.preview
    dup = preview.duplicate_result
    rows = sorted(_rows(preview, dup.clean, "nuevo") + _rows(preview, dup.needs_review, "revisar"),
                  key=lambda r: (r["date"] or date.min, r["idx"]))
    skipped = _rows(preview, dup.auto_skip, "omitido")
    dates = [r["date"] for r in rows + skipped if r["date"]]
    account = db.get(Account, item.account_id)
    return render(request, "importar_preview.html", {
        "token": token, "item": item, "account": account, "rows": rows, "skipped": skipped,
        "n_clean": len(dup.clean), "n_review": len(dup.needs_review), "n_skip": len(dup.auto_skip),
        "n_total": len(preview.df), "warnings": preview.validation_result.warnings,
        "iban": preview.validation_result.iban, "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
    }, user=user, active="importar")


@router.post("/importar/{token}/confirmar")
def import_confirm(request: Request, token: str, importar: List[str] = Form([]),
                   user: WebUser = USER, db: Session = DB) -> Response:
    from app.ingestion.import_service import run_import   # loaded already: a preview exists

    item = _pending(request, user, token)
    review = set(item.preview.duplicate_result.needs_review)
    chosen = {parse_int(v) for v in importar}
    decisions = {idx: idx in chosen for idx in review}
    store = _store(request)
    try:
        result = run_import(db, user.id, item.preview, item.account_id, decisions)
    except LookupError:
        store.discard(token)
        raise HTTPException(status_code=404)
    store.discard(token)
    summary = result.summary
    audit(db, request, user, "import_batches", summary.import_batch_id, AuditAction.insert,
          new=f"extracto «{item.filename}»: {summary.rows_imported} importados, "
              f"{summary.rows_skipped} omitidos, {result.auto_categorized} categorizados")
    notify_change(request, user, "transactions")
    return RedirectResponse(f"/importar/hecho/{summary.import_batch_id}?auto={result.auto_categorized}",
                            status_code=303)


@router.post("/importar/{token}/cancelar")
def import_cancel(request: Request, token: str, user: WebUser = USER) -> Response:
    if _store(request).get(token, user.id, user.session.sid) is not None:
        _store(request).discard(token)
    return RedirectResponse("/importar", status_code=303)


@router.get("/importar/hecho/{batch_id}")
def import_done(request: Request, batch_id: int, auto: int = 0, user: WebUser = USER, db: Session = DB) -> Response:
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.user_id != user.id:
        raise HTTPException(status_code=404)
    first, last = db.query(func.min(Transaction.transaction_date), func.max(Transaction.transaction_date)).filter(
        Transaction.user_id == user.id, Transaction.import_batch_id == batch.id).one()
    return render(request, "importar_hecho.html", {
        "batch": batch, "auto": max(0, min(auto, batch.rows_imported)), "first": first, "last": last,
    }, user=user, active="importar")
