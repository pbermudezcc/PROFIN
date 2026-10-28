"""Login / logout and joining the family with an invitation code (/registro)
for the LAN web front-end.

/registro has no session, hence no CSRF token: like /login it is protected
by the same-origin middleware, SameSite=Strict cookies and LoginLimiter.
Every wrong code counts against the client IP and a shared «invitation»
key (10 failures → 15 min), so guessing a 39-bit code is out of reach.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth.account_rules import (
    MAX_PASSWORD_LENGTH,
    MAX_USERNAME_LENGTH,
    MIN_PASSWORD_LENGTH,
    AccountError,
    validate_new_user,
    validate_username,
)
from app.auth.auth_service import create_user, verify_login
from app.db.models import AuditAction
from app.web.deps import DB, USER, WebUser, client_ip, optional_user
from app.web.invites import format_code, normalize_code
from app.web.render import audit, render
from app.web.security import ABSOLUTE_TIMEOUT, SESSION_COOKIE, too_many_attempts

logger = logging.getLogger(__name__)
router = APIRouter()

GENERIC_ERROR = "Usuario o contraseña incorrectos."
BAD_CODE = "Código no válido o caducado. Pide uno nuevo a quien te invitó."
# LoginLimiter key shared by every invitation attempt ("#" cannot be part of
# a user name, so it never collides with a member's login throttle).
INVITE_KEY = "#invitacion"


def _set_session_cookie(response: Response, sid: str) -> None:
    # __Host- prefix: the browser only accepts it with Secure, Path=/ and no
    # Domain, so no other host on the LAN can set or read it.
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=ABSOLUTE_TIMEOUT, path="/",
        secure=True, httponly=True, samesite="strict",
    )


def _start_session(request: Request, user_id: int, username: str) -> Response:
    sessions = request.app.state.sessions
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        sessions.revoke(previous)          # never reuse a pre-login id
    web_session = sessions.create(user_id, username, client_ip(request), request.headers.get("user-agent", ""))
    return web_session


@router.get("/login")
def login_page(request: Request, borrada: str = "") -> Response:
    if optional_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    # ?borrada=1 confirms an account deleted from «Mi cuenta».
    return render(request, "login.html", {"deleted": borrada == "1"})


@router.post("/login")
def login(
    request: Request,
    username: str = Form("", max_length=64),
    password: str = Form("", max_length=256),
    db: Session = DB,
) -> Response:
    ip = client_ip(request)
    limiter = request.app.state.limiter
    wait = limiter.retry_after(ip, username)
    if wait:
        logger.warning("Web: login bloqueado temporalmente (ip=%s, %ds)", ip, wait)
        return render(request, "login.html", {"error": too_many_attempts(wait), "username": username},
                      status_code=429)

    user = verify_login(db, username, password)
    if user is None:
        limiter.failure(ip, username)
        logger.warning("Web: login fallido (ip=%s)", ip)
        return render(request, "login.html", {"error": GENERIC_ERROR, "username": username},
                      status_code=401)

    limiter.success(ip, username)
    web_session = _start_session(request, user.id, user.username)
    logger.info("Web: login correcto de '%s' (ip=%s)", user.username, ip)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, web_session.sid)
    return response


@router.post("/logout")
def logout(request: Request, user: WebUser = USER) -> Response:
    request.app.state.sessions.revoke(user.session.sid)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@router.post("/logout/todas")
def logout_everywhere(request: Request, user: WebUser = USER) -> Response:
    count = request.app.state.sessions.revoke_user(user.id)
    logger.info("Web: '%s' cerró %d sesiones", user.username, count)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


# ---------------------------------------------------------------------------
# Joining with an invitation code
# ---------------------------------------------------------------------------

def _register_page(request: Request, status_code: int = 200, **context) -> Response:
    ctx = {"step": "code", "errors": {}, "error": None, "username": "", "code": "",
           "min_password": MIN_PASSWORD_LENGTH, "max_password": MAX_PASSWORD_LENGTH,
           "max_username": MAX_USERNAME_LENGTH}
    ctx.update(context)
    return render(request, "registro.html", ctx, status_code=status_code)


def _invite_throttled(request: Request):
    wait = request.app.state.limiter.retry_after(client_ip(request), INVITE_KEY)
    if wait:
        logger.warning("Web: alta por invitación bloqueada temporalmente (ip=%s, %ds)", client_ip(request), wait)
        return too_many_attempts(wait)
    return None


def _bad_code(request: Request) -> None:
    request.app.state.limiter.failure(client_ip(request), INVITE_KEY)
    logger.warning("Web: código de invitación no válido (ip=%s)", client_ip(request))   # never the code


@router.get("/registro")
def register_page(request: Request) -> Response:
    if optional_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _register_page(request)


@router.post("/registro/codigo")
def register_code(request: Request, codigo: str = Form("", max_length=32)) -> Response:
    blocked = _invite_throttled(request)
    if blocked:
        return _register_page(request, status_code=429, error=blocked)
    invite = request.app.state.invites.peek(codigo)
    if invite is None:
        _bad_code(request)
        return _register_page(request, status_code=400, error=BAD_CODE, code=codigo)
    return _register_page(request, step="account", inviter=invite.created_by_name,
                          code=format_code(normalize_code(codigo)))


@router.get("/registro/disponible")
def register_name_hint(request: Request, username: str = "", codigo: str = "", db: Session = DB) -> Response:
    """Live name hint on step 2. Only answers to a valid code (it would
    otherwise tell anyone on the Wi-Fi which names exist); wrong codes count
    in the throttle like on step 1."""
    if _invite_throttled(request):
        return Response(status_code=429)
    if request.app.state.invites.peek(codigo) is None:
        _bad_code(request)
        return Response(status_code=403)
    name = username.strip()[:MAX_USERNAME_LENGTH + 1]
    error = None
    if name:
        try:
            name = validate_username(db, name)
        except AccountError as exc:
            error = exc.message
    return render(request, "partials/user_name_hint.html", {"name": name, "error": error})


@router.post("/registro")
def register(
    request: Request,
    codigo: str = Form("", max_length=32),
    username: str = Form("", max_length=200),
    password: str = Form("", max_length=256),
    confirm: str = Form("", max_length=256),
    db: Session = DB,
) -> Response:
    blocked = _invite_throttled(request)
    if blocked:
        return _register_page(request, status_code=429, error=blocked)
    invites = request.app.state.invites
    invite = invites.peek(codigo)          # checked again: the hidden field is user input
    if invite is None:
        _bad_code(request)
        return _register_page(request, status_code=400, error=BAD_CODE)

    # 1) The fields, without spending the invitation.
    errors = validate_new_user(db, username, password, confirm)
    if errors:
        return _register_page(request, step="account", inviter=invite.created_by_name,
                              code=format_code(normalize_code(codigo)), username=username, errors=errors)

    # 2) Take the invitation (single use), then create the account; give the
    #    invitation back if the account cannot be created after all.
    taken = invites.consume(codigo)
    if taken is None:                      # someone else used it a moment ago
        return _register_page(request, status_code=400, error=BAD_CODE)
    try:
        new_user = create_user(db, username, password)
    except AccountError as exc:
        invites.restore(taken)
        return _register_page(request, step="account", inviter=taken.created_by_name,
                              code=format_code(normalize_code(codigo)), username=username,
                              errors={exc.field or "username": exc.message})
    except Exception:
        invites.restore(taken)
        db.rollback()
        raise

    web_session = _start_session(request, new_user.id, new_user.username)
    audit(db, request, WebUser(new_user.id, new_user.username, web_session), "users", new_user.id,
          AuditAction.insert, new=f"alta por invitación de «{taken.created_by_name}»")
    callback = getattr(request.app.state, "on_change", None)
    if callback is not None:
        callback(new_user.id, "user_created")          # the PC adds the name to its login list
    logger.info("Web: alta por invitación de '%s' (invitado por '%s', ip=%s)",
                new_user.username, taken.created_by_name, client_ip(request))
    response = RedirectResponse("/mas?bienvenida=1", status_code=303)
    _set_session_cookie(response, web_session.sid)
    return response
