"""
app/web/deps.py
===============
FAMILYFIN — FastAPI dependencies shared by every router.

The logged-in user is ALWAYS taken from the server-side session, never
from the URL or a form, so one family member can never read or change
another's data by editing an id.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import database
from app.web.security import CSRF_FIELD, CSRF_HEADER, SAFE_METHODS, SESSION_COOKIE, WebSession, tokens_match


logger = logging.getLogger(__name__)


class LoginRequired(Exception):
    """Raised when a page needs a session; handled in app.py (redirect / 401)."""


class CsrfFailed(Exception):
    """Raised on a state-changing request without the session's CSRF token."""


@dataclass
class WebUser:
    id: int
    username: str
    session: WebSession


def get_db() -> Iterator[Session]:
    # database.SessionLocal is read at call time: switching the data folder
    # rebinds it (refresh_runtime_paths) while the server keeps running.
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def optional_user(request: Request) -> Optional[WebUser]:
    web_session = request.app.state.sessions.get(request.cookies.get(SESSION_COOKIE))
    if web_session is None:
        return None
    return WebUser(web_session.user_id, web_session.username, web_session)


async def current_user(request: Request) -> WebUser:
    user = optional_user(request)
    if user is None:
        raise LoginRequired()
    if request.method not in SAFE_METHODS:
        token = request.headers.get(CSRF_HEADER)
        if token is None and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            token = (await request.form()).get(CSRF_FIELD)
        if not tokens_match(user.session.csrf, token if isinstance(token, str) else None):
            raise CsrfFailed()
    request.state.user = user
    return user


def is_family_member(db: Session, user: WebUser) -> bool:
    """False for the demo account (shown to visitors): it never sees family
    members, invitations or account management."""
    from app.categorization.rules_engine import shares_global_rules
    return shares_global_rules(db, user.id)


def require_family_member(db: Session, user: WebUser) -> None:
    if not is_family_member(db, user):
        logger.warning("Web: la cuenta de demostración intentó gestionar cuentas (%s)", user.username)
        raise HTTPException(status_code=403, detail="No disponible en la cuenta de demostración")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def notify_change(request: Request, user: WebUser, area: str) -> None:
    """Tell the desktop app that web data changed (reloads its views)."""
    callback = getattr(request.app.state, "on_change", None)
    if callback is not None:
        callback(user.id, area)


DB = Depends(get_db)
USER = Depends(current_user)
