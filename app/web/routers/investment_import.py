"""Inversiones: import a DEGIRO account statement from the web (W9d).

* GET  /inversiones/importar                    upload form
* POST /importar/inversiones                    upload → parse → token
* GET  /importar/inversiones/{token}            waiting page or editable preview
* POST /importar/inversiones/{token}/estado     poll a background step (htmx)
* POST /importar/inversiones/{token}/confirmar  symbols chosen → fetch data → import
* POST /importar/inversiones/{token}/cancelar
* GET  /importar/inversiones/{token}/hecho      result

The upload lives under /importar so it gets the upload body cap. The file is
parsed in a private temp folder that is deleted right away; the parsed
statement stays in memory under a random token bound to the user and the
browser session. Calls to Yahoo Finance (symbol suggestion, data of new
securities) run in a background thread and the page polls; the request is
never blocked on the network.

No duplicates: operations whose DEGIRO reference the user already has are
left out of the preview, ledger.import_drafts skips them again at write
time (another import may have happened meanwhile) and the database has a
unique index on (user, broker, broker_ref). A confirmed statement cannot be
confirmed twice: each pending import moves through its states under a lock.

MyInvestor statements need the movement kinds and estimated quantities
reviewed one by one: for now they are imported from the PC app only.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AuditAction
from app.investments.importers.base import KIND_BUY, KIND_IGNORE, KIND_SELL
from app.investments.importers.degiro import BROKER as DEGIRO, StatementError
from app.investments.importers.service import (
    ImportSummary,
    already_imported,
    apply_import,
    catalogue_isins,
    fetch_security_infos,
    parse_any,
    statement_cash_changes,
    symbols_to_fetch,
)
from app.investments.importers.symbols import listing_currency, suggest_symbols
from app.investments.market_data import MarketDataError
from app.investments.portfolio import LedgerError
from app.web.deps import DB, USER, WebUser, notify_change
from app.web.price_refresh import market_provider, start_price_refresh
from app.web.render import audit, is_htmx, render
from app.web.uploads import PendingImports, remove_directory

logger = logging.getLogger(__name__)
router = APIRouter()

TTL = 30 * 60
MAX_PER_USER = 3
MAX_FILE_BYTES = 5 * 1024 * 1024
EXTENSIONS = (".xlsx", ".csv")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,19}$")

# States of a pending import.
SEARCHING = "searching"      # symbol suggestions in the background
REVIEW = "review"            # preview shown, waiting for the user
FETCHING = "fetching"        # data of new securities in the background
APPLYING = "applying"        # writing (one request at a time)
DONE = "done"


class UploadRejected(ValueError):
    pass


@dataclass
class NewOperations:
    """The statement restricted to the operations the user does not have yet
    (what service.apply_import needs: broker, rows, products)."""
    broker: str
    rows: list
    products: Dict[str, dict]
    cash: Dict[str, float] = field(default_factory=dict)    # the statement's newest balances
    cash_as_of: Optional[object] = None


@dataclass
class PendingStatement:
    token: str
    user_id: int
    sid: str
    filename: str
    statement: object                     # degiro.ParsedStatement
    pending: NewOperations
    known: int                            # operations already imported at upload time
    keys: List[str]                       # products of the new operations, display order
    state: str = SEARCHING
    job: Optional[Future] = None
    choices: Dict[str, object] = field(default_factory=dict)    # key → SymbolChoice
    symbols: Dict[str, str] = field(default_factory=dict)       # key → symbol typed
    error: Optional[str] = None
    summary: Optional[ImportSummary] = None
    cash_changes: Dict[str, tuple] = field(default_factory=dict)  # currency → (stored, statement)
    added: int = 0                        # operations written (result page)
    days: Optional[tuple] = None          # (first, last) trade date written
    created: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > TTL


class PendingStatements:
    """Parsed broker statements waiting for confirmation (memory only), and
    the worker threads that talk to the market-data provider."""

    def __init__(self, workers: int = 2) -> None:
        self._items: Dict[str, PendingStatement] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="familyfin-web-inv")

    def add(self, item: PendingStatement) -> str:
        with self._lock:
            self._sweep_locked()
            mine = sorted((p for p in self._items.values() if p.user_id == item.user_id),
                          key=lambda p: p.created)
            for old in mine[: max(0, len(mine) - MAX_PER_USER + 1)]:
                self._items.pop(old.token, None)
            self._items[item.token] = item
        return item.token

    def get(self, token: str, user_id: int, sid: str) -> Optional[PendingStatement]:
        with self._lock:
            self._sweep_locked()
            item = self._items.get(token)
            if item is None or item.user_id != user_id or item.sid != sid:
                return None
            return item

    def discard(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)

    def submit(self, fn, *args) -> Future:
        return self._executor.submit(fn, *args)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def close(self) -> None:
        self.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _sweep_locked(self) -> None:
        for token in [t for t, p in self._items.items() if p.expired]:
            self._items.pop(token, None)


def _store(request: Request) -> PendingStatements:
    return request.app.state.investment_imports


def _provider(request: Request):
    return market_provider(request.app)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _safe_name(filename: Optional[str]) -> str:
    name = Path(filename or "").name
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^\w .()-]", "_", stem).strip(" .")[:80] or "extracto"
    return f"{stem}{ext}"


def _read_upload(upload: UploadFile) -> tuple[str, bytes]:
    name = _safe_name(upload.filename)
    ext = Path(name).suffix
    if ext not in EXTENSIONS:
        raise UploadRejected("El fichero debe ser el «Estado de cuenta» de DEGIRO en Excel (.xlsx) o CSV.")
    data = upload.file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise UploadRejected("El fichero es demasiado grande (máximo 5 MB).")
    if not data:
        raise UploadRejected("El fichero está vacío.")
    if ext == ".xlsx" and not data.startswith(b"PK\x03\x04"):
        raise UploadRejected("El fichero no es un Excel válido.")
    if ext == ".csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadRejected("El CSV no está en UTF-8. Descárgalo de nuevo desde DEGIRO.") from None
        if "\x00" in text:
            raise UploadRejected("El fichero no es un CSV válido.")
    return name, data


def _upload_page(request: Request, user: WebUser, error: Optional[str] = None,
                 status_code: int = 200) -> Response:
    return render(request, "inv_importar.html", {
        "error": error, "max_mb": MAX_FILE_BYTES // (1024 * 1024),
    }, user=user, active="inversiones", status_code=status_code)


@router.get("/inversiones/importar")
def upload_page(request: Request, user: WebUser = USER) -> Response:
    return _upload_page(request, user)


def _new_operations(statement, known_refs: set) -> NewOperations:
    rows = [r for r in statement.rows if r.ref not in known_refs and r.kind != KIND_IGNORE]
    keys = {r.key for r in rows}
    products = {k: v for k, v in statement.products.items() if k in keys}
    return NewOperations(broker=statement.broker, rows=rows, products=products,
                         cash=dict(getattr(statement, "cash", None) or {}),
                         cash_as_of=getattr(statement, "cash_as_of", None))


@router.post("/importar/inversiones")
def upload(request: Request, fichero: UploadFile = File(...), user: WebUser = USER,
           db: Session = DB) -> Response:
    try:
        name, data = _read_upload(fichero)
    except UploadRejected as exc:
        return _upload_page(request, user, error=str(exc), status_code=400)
    # Private temp folder (swept with the CaixaBank uploads if the PC crashes).
    directory = PendingImports.new_directory()
    try:
        path = directory / name
        path.write_bytes(data)
        statement = parse_any(path)
    except StatementError as exc:
        return _upload_page(request, user, error=str(exc), status_code=400)
    except Exception:
        logger.exception("Web: error inesperado al leer un extracto de bróker")
        return _upload_page(request, user, status_code=400,
                            error="No se pudo leer el extracto. ¿Es el «Estado de cuenta» de DEGIRO?")
    finally:
        remove_directory(directory)
    if statement.broker != DEGIRO:
        return _upload_page(request, user, status_code=400, error=(
            f"Desde la web, de momento, solo se importa el «Estado de cuenta» de DEGIRO. Los "
            f"movimientos de {statement.broker} se importan desde la aplicación del PC, donde se "
            "revisa el tipo de cada movimiento y las acciones estimadas."))

    known_refs = already_imported(db, user.id, statement)
    pending = _new_operations(statement, known_refs)
    keys = sorted(pending.products, key=lambda k: pending.products[k]["name"].lower())
    item = PendingStatement(
        token=secrets.token_urlsafe(18), user_id=user.id, sid=user.session.sid, filename=name,
        statement=statement, pending=pending, keys=keys,
        known=sum(1 for r in statement.rows if r.ref in known_refs),
        cash_changes=statement_cash_changes(db, user.id, statement))
    if pending.products:
        item.job = _store(request).submit(
            suggest_symbols, _provider(request), pending.products, catalogue_isins(db))
    else:
        item.state = REVIEW           # nothing new: show it straight away
    _store(request).add(item)
    return RedirectResponse(f"/importar/inversiones/{item.token}", status_code=303)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def _pending(request: Request, user: WebUser, token: str) -> PendingStatement:
    item = _store(request).get(token, user.id, user.session.sid)
    if item is None:
        raise HTTPException(status_code=404)
    return item


def _symbol_rows(item: PendingStatement) -> List[dict]:
    rows = []
    for index, key in enumerate(item.keys):
        info = item.pending.products[key]
        choice = item.choices.get(key)
        chosen = item.symbols.get(key) or (choice.chosen if choice else "") or ""
        candidates = list(choice.candidates) if choice else []
        best = next((c for c in candidates if c.symbol == chosen), None)
        warn = False
        if choice is not None and choice.from_catalogue:
            detail = "Ya está en el catálogo"
        elif best is None and chosen:
            listing = listing_currency(chosen)
            warn = bool(listing and listing != info["currency"])
            detail = "Cotiza en otra divisa que la compra" if warn else "Elegido a mano"
        elif best is None:
            detail, warn = "Sin coincidencia: escribe el símbolo de Yahoo", True
        elif best.gap is not None:
            detail = f"{best.exchange} · precio {best.gap_text}"
        else:
            detail = f"{best.name} · {best.exchange} · compruébalo"
        rows.append({
            "index": index, "key": key, "name": info["name"], "currency": info["currency"],
            "symbol": chosen, "detail": detail, "warn": warn,
            "candidates": [(c.symbol, f"{c.name} · {c.exchange} · {c.gap_text}") for c in candidates],
            "operations": sum(1 for r in item.pending.rows if r.key == key),
        })
    return rows


def _operation_rows(item: PendingStatement) -> List[dict]:
    rows = []
    for r in sorted(item.pending.rows, key=lambda r: (r.day, r.ref), reverse=True):
        trade = r.kind in (KIND_BUY, KIND_SELL)
        rows.append({
            "day": r.day, "name": r.name, "kind": r.kind, "trade": trade,
            "quantity": r.quantity if trade else None,
            "price": r.price if trade else None, "currency": r.currency,
            "amount": r.amount_eur, "sign": -1 if r.kind == KIND_BUY else 1,
            "costs": r.fees_eur if trade else r.withheld_eur,
            "detail": r.gross_text if not trade else (r.note or ""),
        })
    return rows


def _preview(request: Request, user: WebUser, item: PendingStatement,
             status_code: int = 200) -> Response:
    st = item.statement
    trades = sum(1 for r in item.pending.rows if r.kind in (KIND_BUY, KIND_SELL))
    return render(request, "inv_importar_preview.html", {
        "token": item.token, "item": item, "first": st.first_day, "last": st.last_day,
        "n_trades": trades, "n_dividends": len(item.pending.rows) - trades,
        "n_known": item.known, "symbols": _symbol_rows(item), "rows": _operation_rows(item),
        "ignored": sorted(st.ignored.items(), key=lambda kv: -kv[1]), "warnings": st.warnings,
        "error": item.error, "cash_changes": item.cash_changes,
    }, user=user, active="inversiones", status_code=status_code)


def _waiting(request: Request, user: WebUser, item: PendingStatement) -> Response:
    return render(request, "inv_importar_espera.html", {
        "token": item.token, "item": item, "importing": item.state == FETCHING,
    }, user=user, active="inversiones")


def _advance(request: Request, user: WebUser, db: Session, item: PendingStatement) -> None:
    """Move a finished background step on. Called with item.lock held."""
    if item.job is None or not item.job.done():
        return
    job, item.job = item.job, None
    if item.state == SEARCHING:
        try:
            item.choices = job.result()
        except Exception as exc:
            logger.info("Web: búsqueda de símbolos fallida: %s", exc)
            item.choices = {}
            item.error = (f"No se pudo consultar Yahoo Finance ({exc}). "
                          "Escribe el símbolo de cada valor a mano.")
        item.state = REVIEW
    elif item.state == FETCHING:
        try:
            infos = job.result()
        except Exception as exc:
            message = str(exc) if isinstance(exc, MarketDataError) else f"Error: {exc}"
            item.error = f"No se pudieron obtener los datos de algún valor: {message}"
            item.state = REVIEW
            return
        _apply(request, user, db, item, infos)


def _apply(request: Request, user: WebUser, db: Session, item: PendingStatement, infos) -> None:
    item.state = APPLYING
    try:
        summary = apply_import(db, user.id, item.pending, item.symbols, infos)
    except LedgerError as exc:
        item.error, item.state = f"No se ha importado nada: {exc}", REVIEW
        return
    except IntegrityError:
        # The unique (user, broker, broker_ref) index caught an operation
        # imported meanwhile from another window or the PC app.
        db.rollback()
        item.error, item.state = ("No se ha importado nada: algunas operaciones se acaban de importar "
                                  "desde otra ventana. Vuelve a subir el extracto."), REVIEW
        return
    item.summary, item.state, item.error = summary, DONE, None
    result = summary.result
    # Plain values for the result page: the ORM rows detach with this session.
    days = [t.trade_date for t in result.added]
    item.added, item.days = len(result.added), (min(days), max(days)) if days else None
    audit(db, request, user, "investment_transactions", None, AuditAction.insert,
          new=f"extracto {item.pending.broker} «{item.filename}»: {summary.trades} compras/ventas, "
              f"{summary.dividends} dividendos, {result.skipped} ya importadas"
              + (f", efectivo {item.pending.cash}" if summary.cash_updated else ""))
    notify_change(request, user, "investments")
    if result.added:
        # Like the desktop import: download the history of the new securities.
        start_price_refresh(request.app, user.id)


@router.get("/importar/inversiones/{token}")
def preview(request: Request, token: str, user: WebUser = USER, db: Session = DB) -> Response:
    item = _pending(request, user, token)
    with item.lock:
        if item.state == SEARCHING:       # writes happen only on the POST /estado
            _advance(request, user, db, item)
        state = item.state
    if state == DONE:
        return RedirectResponse(f"/importar/inversiones/{token}/hecho", status_code=303)
    if state in (SEARCHING, FETCHING, APPLYING):
        return _waiting(request, user, item)
    return _preview(request, user, item)


@router.post("/importar/inversiones/{token}/estado")
def status(request: Request, token: str, user: WebUser = USER, db: Session = DB) -> Response:
    item = _pending(request, user, token)
    with item.lock:
        _advance(request, user, db, item)
        busy = item.state in (SEARCHING, FETCHING, APPLYING)
    if busy and is_htmx(request):
        return render(request, "partials/inv_import_poll.html", {"token": token}, user=user)
    target = f"/importar/inversiones/{token}" + ("/hecho" if item.state == DONE else "")
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


def _clean_symbol(value: str) -> str:
    return (value or "").strip().upper()


@router.post("/importar/inversiones/{token}/confirmar")
def confirm(request: Request, token: str, simbolo: List[str] = Form([]),
            user: WebUser = USER, db: Session = DB) -> Response:
    """`simbolo` holds one field per product, in the preview's order (item.keys)."""
    item = _pending(request, user, token)
    with item.lock:
        if item.state == SEARCHING:
            _advance(request, user, db, item)
        if item.state != REVIEW or not (item.pending.rows or item.cash_changes):
            # Already confirmed (double tap, second tab) or nothing to do: never twice.
            return RedirectResponse(f"/importar/inversiones/{token}", status_code=303)
        if len(simbolo) != len(item.keys):
            raise HTTPException(status_code=400)
        symbols, problems = {}, []
        for key, value in zip(item.keys, simbolo):
            symbol = _clean_symbol(value)
            symbols[key] = symbol
            name = item.pending.products[key]["name"]
            if not symbol:
                problems.append(f"Falta el símbolo de «{name}».")
            elif not SYMBOL.match(symbol):
                problems.append(f"El símbolo «{symbol[:30]}» de «{name}» no es válido.")
        item.symbols = symbols
        if problems:
            item.error = " ".join(problems)
            return _preview(request, user, item, status_code=400)
        item.error = None
        to_fetch = symbols_to_fetch(db, symbols.values())
        if to_fetch:
            item.state = FETCHING
            item.job = _store(request).submit(fetch_security_infos, _provider(request), to_fetch)
            return RedirectResponse(f"/importar/inversiones/{token}", status_code=303)
        _apply(request, user, db, item, {})
        if item.state != DONE:
            return _preview(request, user, item, status_code=400)
    return RedirectResponse(f"/importar/inversiones/{token}/hecho", status_code=303)


@router.post("/importar/inversiones/{token}/cancelar")
def cancel(request: Request, token: str, user: WebUser = USER) -> Response:
    item = _store(request).get(token, user.id, user.session.sid)
    if item is not None:
        with item.lock:
            if item.state in (REVIEW, SEARCHING, DONE):
                _store(request).discard(token)
    return RedirectResponse("/inversiones/operaciones", status_code=303)


@router.get("/importar/inversiones/{token}/hecho")
def done(request: Request, token: str, user: WebUser = USER) -> Response:
    item = _pending(request, user, token)
    if item.state != DONE or item.summary is None:
        return RedirectResponse(f"/importar/inversiones/{token}", status_code=303)
    first, last = item.days or (None, None)
    return render(request, "inv_importar_hecho.html", {
        "item": item, "s": item.summary, "added": item.added, "first": first, "last": last,
    }, user=user, active="inversiones")
