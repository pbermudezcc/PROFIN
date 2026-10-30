"""Biblioteca de categorías: export / import of the user's categories and rules.

* GET  /mas/categorias/biblioteca            export button + upload form
* GET  /mas/categorias/exportar               the .profin-cats.json download
* POST /importar/categorias                   upload → server-side preview token
* GET  /importar/categorias/{token}?modo=…    preview (merge / replace)
* POST /importar/categorias/{token}/confirmar apply, audit, refresh the PC app

The upload lives under /importar so it gets the upload body cap. The parsed
file stays on the server under a random token bound to the user and the
browser session (never in the browser). Global rules are never imported
from the web: they are changed only from the PC app.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.categorization import library_io
from app.db.models import AuditAction
from app.web.deps import DB, USER, WebUser, notify_change
from app.web.render import audit, render

router = APIRouter()

TTL = 15 * 60
MAX_PER_USER = 3


@dataclass
class PendingLibrary:
    token: str
    user_id: int
    sid: str
    filename: str
    lib: library_io.LibraryFile
    created: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > TTL


class PendingLibraries:
    """Parsed library files waiting for confirmation (in memory only)."""

    def __init__(self) -> None:
        self._items: Dict[str, PendingLibrary] = {}
        self._lock = threading.Lock()

    def add(self, user_id: int, sid: str, filename: str, lib: library_io.LibraryFile) -> str:
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._sweep_locked()
            mine = sorted((p for p in self._items.values() if p.user_id == user_id), key=lambda p: p.created)
            for old in mine[: max(0, len(mine) - MAX_PER_USER + 1)]:
                self._items.pop(old.token, None)
            self._items[token] = PendingLibrary(token, user_id, sid, filename, lib)
        return token

    def get(self, token: str, user_id: int, sid: str) -> Optional[PendingLibrary]:
        with self._lock:
            self._sweep_locked()
            item = self._items.get(token)
            if item is None or item.user_id != user_id or item.sid != sid:
                return None
            return item

    def discard(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)

    def _sweep_locked(self) -> None:
        for token in [t for t, p in self._items.items() if p.expired]:
            self._items.pop(token, None)


def _store(request: Request) -> PendingLibraries:
    return request.app.state.library_imports


def _page(request: Request, user: WebUser, error: Optional[str] = None, status_code: int = 200) -> Response:
    return render(request, "mas_biblioteca.html", {
        "error": error, "suffix": library_io.FILE_SUFFIX,
        "max_kb": library_io.MAX_FILE_BYTES // 1024,
    }, user=user, active="categorias", status_code=status_code)


@router.get("/mas/categorias/biblioteca")
def library_page(request: Request, user: WebUser = USER) -> Response:
    return _page(request, user)


@router.get("/mas/categorias/exportar")
def library_export(user: WebUser = USER, db: Session = DB) -> Response:
    payload = library_io.export_library(db, user.id, include_global=False, source_user=user.username)
    filename = library_io.suggested_filename(user.username)
    return Response(
        library_io.dumps(payload),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/importar/categorias")
def library_upload(request: Request, fichero: UploadFile = File(...), user: WebUser = USER) -> Response:
    data = fichero.file.read(library_io.MAX_FILE_BYTES + 1)
    if not data:
        return _page(request, user, error="El archivo está vacío.", status_code=400)
    try:
        lib = library_io.parse_library(data)
    except library_io.LibraryError as exc:
        return _page(request, user, error=str(exc), status_code=400)
    name = (fichero.filename or "biblioteca").replace("\\", "/").rsplit("/", 1)[-1][:100]
    token = _store(request).add(user.id, user.session.sid, name, lib)
    return RedirectResponse(f"/importar/categorias/{token}", status_code=303)


def _pending(request: Request, user: WebUser, token: str) -> PendingLibrary:
    item = _store(request).get(token, user.id, user.session.sid)
    if item is None:
        raise HTTPException(status_code=404)
    return item


def _rule_text(rule: library_io.LibRule) -> str:
    return (f"«{rule.pattern}»{library_io.movement_note(rule.transaction_type)} → "
            f"{rule.type} › {rule.category} (prioridad {rule.priority})")


def _mode(value: Optional[str]) -> str:
    return library_io.MODE_REPLACE if value == library_io.MODE_REPLACE else library_io.MODE_MERGE


@router.get("/importar/categorias/{token}")
def library_preview(request: Request, token: str, modo: Optional[str] = None,
                    user: WebUser = USER, db: Session = DB) -> Response:
    item = _pending(request, user, token)
    mode = _mode(modo)
    plan = library_io.plan_import(db, user.id, item.lib, mode, allow_global=False)
    return render(request, "mas_biblioteca_preview.html", {
        "token": token, "item": item, "plan": plan, "mode": mode,
        "new_rules": [_rule_text(r) for r in plan.new_rules],
        "priority_changes": [f"«{r.pattern}»: prioridad {old} → {r.priority}"
                             for r, old in plan.priority_changes],
    }, user=user, active="categorias")


@router.post("/importar/categorias/{token}/confirmar")
def library_confirm(request: Request, token: str, modo: str = Form(library_io.MODE_MERGE),
                    entiendo: str = Form(""), user: WebUser = USER, db: Session = DB) -> Response:
    item = _pending(request, user, token)
    mode = _mode(modo)
    if mode == library_io.MODE_REPLACE and not entiendo and             library_io.plan_import(db, user.id, item.lib, mode).deleted_rules:
        # Deleting rules needs the explicit tick on the preview.
        return RedirectResponse(f"/importar/categorias/{token}?modo={mode}", status_code=303)
    try:
        result = library_io.apply_import(db, user.id, item.lib, mode, allow_global=False)
    except library_io.LibraryError as exc:
        return _page(request, user, error=str(exc), status_code=400)
    _store(request).discard(token)
    audit(db, request, user, "categories", None, AuditAction.insert,
          new=f"biblioteca «{item.filename}» ({mode}): "
              f"{result.types_created + result.categories_created} categorías, "
              f"{result.rules_created} reglas nuevas, {result.rules_updated} actualizadas, "
              f"{result.rules_deleted} borradas")
    notify_change(request, user, "transactions")
    return render(request, "mas_biblioteca_hecho.html", {
        "item": item, "result": result, "mode": mode,
    }, user=user, active="categorias")


@router.post("/importar/categorias/{token}/cancelar")
def library_cancel(request: Request, token: str, user: WebUser = USER) -> Response:
    if _store(request).get(token, user.id, user.session.sid) is not None:
        _store(request).discard(token)
    return RedirectResponse("/mas/categorias/biblioteca", status_code=303)
