"""Importar: CaixaBank statement upload → preview with duplicates → confirm.

One or several statements of one account are uploaded together. Each file
is checked (size, extension, real Excel signature), parsed on the PC in a
private temp folder, and the preview of all of them is kept server-side
under a random token bound to the user and the browser session. Files that
cannot be used are listed with their reason; the rest can still be imported.
Nothing is written until the user confirms; the temp folder is deleted on
confirm, cancel or expiry.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
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
MAX_FILES = 12
MAX_TOTAL_BYTES = 25 * 1024 * 1024        # within security.MAX_STATEMENTS_BODY
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
          account_id: Optional[int] = None, reasons: Sequence[Tuple[str, str]] = ()) -> Response:
    batches = (
        db.query(ImportBatch).filter(ImportBatch.user_id == user.id)
        .order_by(ImportBatch.import_timestamp.desc()).limit(5).all()
    )
    return render(request, "importar.html", {
        "accounts": db.query(Account).filter(Account.user_id == user.id).order_by(Account.id).all(),
        "account_id": account_id, "error": error, "reasons": reasons,
        "batches": batches, "max_mb": MAX_FILE_BYTES // (1024 * 1024),
        "max_files": MAX_FILES, "max_total_mb": MAX_TOTAL_BYTES // (1024 * 1024),
    }, user=user, active="importar")


def _failed(request: Request, db: Session, user: WebUser, account_id: Optional[int],
            rejected: Sequence[Tuple[str, str]]) -> Response:
    """No file of the upload can be used: one reason, or one per file."""
    if len(rejected) == 1:
        return _page(request, db, user, error=rejected[0][1], account_id=account_id)
    return _page(request, db, user, error="Ninguno de los extractos se puede importar:",
                 account_id=account_id, reasons=rejected)


@router.get("/importar")
def import_page(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    return _page(request, db, user)


@router.post("/importar")
def import_upload(
    request: Request,
    ficheros: List[UploadFile] = File(...),
    cuenta: str = Form(""),
    user: WebUser = USER,
    db: Session = DB,
) -> Response:
    account_id = parse_int(cuenta)
    chosen = [f for f in ficheros if f.filename]     # an empty part when nothing was picked
    if not chosen:
        return _page(request, db, user, error="Elige al menos un extracto.", account_id=account_id)
    if len(chosen) > MAX_FILES:
        return _page(request, db, user, error=f"Sube como máximo {MAX_FILES} extractos a la vez.",
                     account_id=account_id)
    accepted: List[Tuple[str, bytes]] = []
    rejected: List[Tuple[str, str]] = []
    total = 0
    for upload in chosen:
        try:
            name, data = _read_upload(upload)
        except UploadRejected as exc:
            rejected.append((_safe_name(upload.filename), str(exc)))
            continue
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            return _page(request, db, user, account_id=account_id,
                         error=f"Los extractos suman más de {MAX_TOTAL_BYTES // (1024 * 1024)} MB. "
                               "Súbelos en varias veces.")
        accepted.append((name, data))
    if not accepted:
        return _failed(request, db, user, account_id, rejected)
    try:
        from app.ingestion.import_service import ensure_default_account
        from app.ingestion.importer import NoUsableStatement, RejectedFile, prepare_import
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
    paths = []
    for n, (name, data) in enumerate(accepted, start=1):
        folder = directory / f"{n:02d}"
        folder.mkdir()
        paths.append(folder / name)
        paths[-1].write_bytes(data)
    try:
        # Sync endpoint: FastAPI runs it in a worker thread, so parsing never
        # blocks the server's event loop.
        preview = prepare_import(paths, db, user.id, account.id)
    except NoUsableStatement as exc:
        remove_directory(directory)
        if rejected or len(exc.rejected) > 1:
            return _failed(request, db, user, account.id, rejected + [(r.name, r.reason) for r in exc.rejected])
        return _page(request, db, user, error=f"No se pudo leer el extracto: {exc}", account_id=account.id)
    except (ValidationError, ValueError) as exc:
        remove_directory(directory)
        return _page(request, db, user, error=f"No se pudo leer el extracto: {exc}", account_id=account.id)
    except Exception:
        remove_directory(directory)
        logger.exception("Web: error inesperado al leer un extracto")
        return _page(request, db, user, error="No se pudo leer el extracto. ¿Es una exportación de CaixaBank?",
                     account_id=account.id)
    preview.rejected[:0] = [RejectedFile(name, reason) for name, reason in rejected]
    token = store.add(user.id, user.session.sid, account.id, [f.name for f in preview.files], directory, preview)
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
            "file": row.get("source_file") or "",
        })
    return out


# Registered before /importar/{token}, which would otherwise take "hecho".
@router.get("/importar/hecho")
def import_done(request: Request, lote: List[int] = Query([]), auto: int = 0,
                user: WebUser = USER, db: Session = DB) -> Response:
    ids = list(dict.fromkeys(lote))
    batches = db.query(ImportBatch).filter(ImportBatch.id.in_(ids), ImportBatch.user_id == user.id).all() if ids else []
    if not ids or len(batches) != len(ids):
        raise HTTPException(status_code=404)
    batches.sort(key=lambda b: ids.index(b.id))
    first, last = db.query(func.min(Transaction.transaction_date), func.max(Transaction.transaction_date)).filter(
        Transaction.user_id == user.id, Transaction.import_batch_id.in_(ids)).one()
    imported = sum(b.rows_imported for b in batches)
    return render(request, "importar_hecho.html", {
        "batches": batches, "imported": imported, "skipped": sum(b.rows_skipped for b in batches),
        "auto": max(0, min(auto, imported)), "first": first, "last": last,
    }, user=user, active="importar")


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
    stats = preview.file_stats()
    return render(request, "importar_preview.html", {
        "token": token, "item": item, "account": account, "rows": rows, "skipped": skipped,
        "n_clean": len(dup.clean), "n_review": len(dup.needs_review), "n_skip": len(dup.auto_skip),
        "n_total": len(preview.df), "warnings": preview.warnings,
        "iban": preview.iban, "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
        "files": [(f, stats[f.name]) for f in preview.files], "rejected": preview.rejected,
        "many": len(preview.files) > 1,
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
    for batch in db.query(ImportBatch).filter(ImportBatch.id.in_(summary.batch_ids)).order_by(ImportBatch.id):
        audit(db, request, user, "import_batches", batch.id, AuditAction.insert,
              new=f"extracto «{batch.filename}»: {batch.rows_imported} importados, {batch.rows_skipped} omitidos"
                  + (f" ({len(summary.batch_ids)} extractos, {result.auto_categorized} categorizados en total)"
                     if len(summary.batch_ids) > 1 else f", {result.auto_categorized} categorizados"))
    notify_change(request, user, "transactions")
    query = urlencode([("lote", i) for i in summary.batch_ids] + [("auto", result.auto_categorized)])
    return RedirectResponse(f"/importar/hecho?{query}", status_code=303)


@router.post("/importar/{token}/cancelar")
def import_cancel(request: Request, token: str, user: WebUser = USER) -> Response:
    if _store(request).get(token, user.id, user.session.sid) is not None:
        _store(request).discard(token)
    return RedirectResponse("/importar", status_code=303)
