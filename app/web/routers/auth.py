"""Login / logout for the LAN web front-end."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth.auth_service import verify_login
from app.web.deps import DB, USER, WebUser, client_ip, optional_user
from app.web.render import render
from app.web.security import ABSOLUTE_TIMEOUT, SESSION_COOKIE

logger = logging.getLogger(__name__)
router = APIRouter()

GENERIC_ERROR = "Usuario o contraseña incorrectos."


def _set_session_cookie(response: Response, sid: str) -> None:
    # __Host- prefix: the browser only accepts it with Secure, Path=/ and no
    # Domain, so no other host on the LAN can set or read it.
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=ABSOLUTE_TIMEOUT, path="/",
        secure=True, httponly=True, samesite="strict",
    )


@router.get("/login")
def login_page(request: Request) -> Response:
    if optional_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


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
        minutes = (wait + 59) // 60
        message = (f"Demasiados intentos. Vuelve a probar en {wait} s."
                   if wait < 60 else f"Demasiados intentos. Vuelve a probar en {minutes} min.")
        return render(request, "login.html", {"error": message, "username": username},
                      status_code=429)

    user = verify_login(db, username, password)
    if user is None:
        limiter.failure(ip, username)
        logger.warning("Web: login fallido (ip=%s)", ip)
        return render(request, "login.html", {"error": GENERIC_ERROR, "username": username},
                      status_code=401)

    limiter.success(ip, username)
    sessions = request.app.state.sessions
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        sessions.revoke(previous)          # never reuse a pre-login id
    web_session = sessions.create(user.id, user.username, ip, request.headers.get("user-agent", ""))
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
