"""Mi cuenta (W10): a member changes their password, renames or deletes
THEIR OWN account. There is no admin role: the user is always the one of
the session, never an id from the URL or the form.

Security on every route:
  * session + CSRF (USER), same-origin check in the middleware;
  * the demo account gets 403 (it is shown to visitors);
  * every password check goes through LoginLimiter with the same keys as
    the login (IP + user name), so a stolen unlocked phone cannot brute-force
    the password from here; at the limit the answer is 429 with the login's
    message;
  * audited, and the desktop is told (notify_change "account" /
    "account_deleted").

The rules live in app.auth.account_service (shared with the PC).
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth.account_rules import (
    MAX_PASSWORD_LENGTH,
    MAX_USERNAME_LENGTH,
    MIN_PASSWORD_LENGTH,
    AccountError,
    validate_username,
)
from app.auth.account_service import (
    WrongPassword,
    change_password,
    delete_user,
    deletion_summary,
    rename_user,
)
from app.db.models import AuditAction
from app.web.deps import DB, USER, WebUser, client_ip, notify_change, require_family_member
from app.web.render import audit, hx_trigger, is_htmx, render
from app.web.security import SESSION_COOKIE, too_many_attempts

logger = logging.getLogger(__name__)
router = APIRouter()


def _set_cookie(response: Response, sid: str) -> None:
    from app.web.routers.auth import _set_session_cookie
    _set_session_cookie(response, sid)


def _throttled(request: Request, user: WebUser) -> Optional[str]:
    """Message when this IP or user name must wait, else None."""
    wait = request.app.state.limiter.retry_after(client_ip(request), user.username)
    if wait:
        logger.warning("Web: verificación de contraseña bloqueada (ip=%s, %ds)", client_ip(request), wait)
        return too_many_attempts(wait)
    return None


def _count(request: Request, user: WebUser, exc: Optional[AccountError]) -> None:
    """Feed the login throttle: wrong passwords count, a right one clears it."""
    limiter = request.app.state.limiter
    if isinstance(exc, WrongPassword):
        limiter.failure(client_ip(request), user.username)
        logger.warning("Web: contraseña incorrecta en «Mi cuenta» de '%s' (ip=%s)", user.username, client_ip(request))
    elif exc is None:
        limiter.success(client_ip(request), user.username)


def _sheet(request: Request, user: WebUser, template: str, context: Dict, status_code: int = 200) -> Response:
    ctx = {"min_password": MIN_PASSWORD_LENGTH, "max_password": MAX_PASSWORD_LENGTH,
           "max_username": MAX_USERNAME_LENGTH, "errors": {}, "error": None}
    ctx.update(context)
    return render(request, template, ctx, user=user, status_code=status_code)


def _errors(exc: AccountError) -> Dict[str, str]:
    return {exc.field: exc.message} if exc.field else {}


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

@router.get("/mas/cuenta/contrasena")
def password_form(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    require_family_member(db, user)
    return _sheet(request, user, "partials/account_password.html", {})


@router.post("/mas/cuenta/contrasena")
def password_change(
    request: Request,
    current: str = Form("", max_length=256),
    password: str = Form("", max_length=256),
    confirm: str = Form("", max_length=256),
    user: WebUser = USER,
    db: Session = DB,
) -> Response:
    require_family_member(db, user)
    blocked = _throttled(request, user)
    if blocked:
        return _sheet(request, user, "partials/account_password.html", {"error": blocked}, status_code=429)
    try:
        change_password(db, user.id, current, password, confirm)
    except AccountError as exc:
        _count(request, user, exc)
        return _sheet(request, user, "partials/account_password.html",
                      {"errors": _errors(exc), "error": None if exc.field else exc.message})
    _count(request, user, None)

    sessions = request.app.state.sessions
    closed = sessions.revoke_user(user.id, keep=user.session.sid)
    fresh = sessions.rotate(user.session.sid)
    audit(db, request, user, "users", user.id, AuditAction.update,
          new=f"contraseña cambiada; {closed} sesiones cerradas")
    notify_change(request, user, "account")
    logger.info("Web: '%s' cambió su contraseña (%d sesiones cerradas)", user.username, closed)

    message = "Contraseña cambiada"
    if closed:
        message += f" · se cerró la sesión en {closed} dispositivo{'s' if closed != 1 else ''}"
    response = Response(status_code=204)
    if fresh is not None:
        _set_cookie(response, fresh.sid)
    if is_htmx(request):
        # The page keeps working: the new CSRF token replaces the old one.
        hx_trigger(response, toast=message, closeSheet=True, refreshPage=True,
                   csrfToken=fresh.csrf if fresh else "")
    else:
        response.status_code = 303
        response.headers["Location"] = "/mas"
    return response


# ---------------------------------------------------------------------------
# User name
# ---------------------------------------------------------------------------

@router.get("/mas/cuenta/nombre")
def rename_form(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    require_family_member(db, user)
    return _sheet(request, user, "partials/account_rename.html", {"username": ""})


@router.get("/mas/cuenta/nombre/disponible")
def rename_hint(request: Request, username: str = "", user: WebUser = USER, db: Session = DB) -> Response:
    """Live hint under the new-name field (name rules only, no password)."""
    require_family_member(db, user)
    name = username.strip()[:MAX_USERNAME_LENGTH + 1]
    error = None
    if name:
        try:
            name = validate_username(db, name, exclude_id=user.id)
            if name == user.username:
                error = "Es tu nombre actual."
        except AccountError as exc:
            error = exc.message
    return render(request, "partials/user_name_hint.html", {
        "name": name, "error": error, "idle": "Con este nombre entrarás en el PC y en la web.",
    }, user=user)


@router.post("/mas/cuenta/nombre")
def rename(
    request: Request,
    username: str = Form("", max_length=200),
    password: str = Form("", max_length=256),
    user: WebUser = USER,
    db: Session = DB,
) -> Response:
    require_family_member(db, user)
    blocked = _throttled(request, user)
    if blocked:
        return _sheet(request, user, "partials/account_rename.html",
                      {"username": username, "error": blocked}, status_code=429)
    try:
        old, new = rename_user(db, user.id, password, username)
    except AccountError as exc:
        _count(request, user, exc)
        return _sheet(request, user, "partials/account_rename.html",
                      {"username": username, "errors": _errors(exc), "error": None if exc.field else exc.message})
    _count(request, user, None)

    request.app.state.sessions.rename_user(user.id, new)
    request.app.state.invites.rename_member(user.id, new)
    user.username = new
    audit(db, request, user, "users", user.id, AuditAction.update, old=f"«{old}»", new=f"«{new}»")
    notify_change(request, user, "account")
    logger.info("Web: '%s' se renombró a '%s'", old, new)
    response = Response(status_code=204)
    if is_htmx(request):
        hx_trigger(response, toast=f"Ahora tu usuario es «{new}»", closeSheet=True, refreshPage=True)
    else:
        response.status_code = 303
        response.headers["Location"] = "/mas"
    return response


# ---------------------------------------------------------------------------
# Delete my account
# ---------------------------------------------------------------------------

def _delete_sheet(request: Request, user: WebUser, db: Session, status_code: int = 200, **context) -> Response:
    return _sheet(request, user, "partials/account_delete.html",
                  {"summary": deletion_summary(db, user.id), **context}, status_code=status_code)


@router.get("/mas/cuenta/borrar")
def delete_form(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    require_family_member(db, user)
    return _delete_sheet(request, user, db)


@router.post("/mas/cuenta/borrar")
def delete_account(
    request: Request,
    confirm_name: str = Form("", max_length=200),
    password: str = Form("", max_length=256),
    user: WebUser = USER,
    db: Session = DB,
) -> Response:
    require_family_member(db, user)
    backup_fn = request.app.state.backup_before_delete
    if backup_fn is None:
        raise HTTPException(status_code=503, detail="Borrar cuentas solo es posible con la aplicación del PC")
    blocked = _throttled(request, user)
    if blocked:
        return _delete_sheet(request, user, db, status_code=429, error=blocked, confirm_name=confirm_name)

    name = user.username
    try:
        backup = delete_user(db, user.id, password, confirm_name, backup=lambda: backup_fn(name), source="la web")
    except AccountError as exc:
        _count(request, user, exc)
        db.rollback()
        return _delete_sheet(request, user, db, errors=_errors(exc),
                             error=None if exc.field else exc.message, confirm_name=confirm_name)
    _count(request, user, None)

    state = request.app.state
    closed = state.sessions.revoke_user(user.id)
    state.invites.revoke_by(user.id)
    notify_change(request, user, "account_deleted")
    logger.warning("Web: '%s' borró su cuenta (ip=%s, %d sesiones cerradas, copia %s)",
                   name, client_ip(request), closed, backup.name)

    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    if is_htmx(request):
        response.headers["HX-Redirect"] = "/login?borrada=1"
    else:
        response.status_code = 303
        response.headers["Location"] = "/login?borrada=1"
    return response
