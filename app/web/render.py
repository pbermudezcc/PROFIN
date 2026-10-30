"""
app/web/render.py
=================
FAMILYFIN — Template rendering and small shared helpers for the routers.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db.models import AuditAction, AuditLog
from app.web.deps import WebUser, client_ip

logger = logging.getLogger(__name__)

# (key, label, href, icon id in static/icons/sprite.svg)
NAV = [
    ("resumen", "Resumen", "/", "home"),
    ("movimientos", "Movimientos", "/movimientos", "list"),
    ("flujos", "Gastos e ingresos", "/gastos", "pie"),
    ("prevision", "Previsión", "/prevision", "calendar"),
    ("inversiones", "Inversiones", "/inversiones", "trend"),
    ("mas", "Más", "/mas", "more"),
]
# Sidebar only (tablets): the desktop's bottom NAV_ITEMS group. On phones
# these pages live under the «Más» tab.
ADMIN_NAV = [
    ("importar", "Importar extracto", "/importar", "upload"),
    ("categorias", "Categorías", "/mas/categorias", "tag"),
    ("reglas", "Reglas", "/mas/reglas", "rules"),
    ("cuentas", "Cuentas", "/mas/cuentas", "bank"),
]
ADMIN_KEYS = {key for key, *_ in ADMIN_NAV}
# Sidebar only: the phone tab bar keeps five tabs; on phones these pages
# open from «Más» (and its tab is highlighted).
SIDEBAR_ONLY = {"prevision"}


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def render(
    request: Request,
    template: str,
    context: Optional[Dict[str, Any]] = None,
    user: Optional[WebUser] = None,
    active: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    ctx: Dict[str, Any] = {
        "user": user,
        "csrf": user.session.csrf if user else "",
        "nav": NAV,
        "admin_nav": ADMIN_NAV,
        "active": active,
        # The phone tab bar has no admin entries: they light up «Más».
        "tab_active": "mas" if active in ADMIN_KEYS | SIDEBAR_ONLY else active,
        "today": date.today(),
    }
    ctx.update(context or {})
    return request.app.state.templates.TemplateResponse(request, template, ctx, status_code=status_code)


def audit(
    session: Session,
    request: Request,
    user: WebUser,
    table: str,
    record_id: Optional[int],
    action: AuditAction,
    old: Optional[str] = None,
    new: Optional[str] = None,
) -> None:
    """Record a web write in audit_log (who, from which device IP, what)."""
    try:
        session.add(AuditLog(
            user_id=user.id,
            table_affected=table,
            record_id=record_id,
            action=action,
            old_value=old,
            new_value=f"{new or ''} [web {client_ip(request)}]".strip(),
        ))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Web: no se pudo escribir en audit_log")


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_float(value: Optional[str]) -> Optional[float]:
    """Accepts '1.234,56', '1234,56' and '1234.56'."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace("€", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def hx_trigger(response, **events) -> None:
    """Fire client events (toast, closeSheet…) after an htmx swap. ASCII-only
    JSON: header values travel as latin-1."""
    response.headers["HX-Trigger"] = json.dumps(events, ensure_ascii=True)
