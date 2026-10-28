"""Familia (Ajustes): family members and invitation codes (W10).

Nobody creates accounts for others. A signed-in member issues a single-use
code (15 min) and the new person redeems it on the login page (/registro,
routers/auth.py) choosing their own name and password. The demo account
(shown to visitors) cannot list members nor issue invitations (403).

The code is shown once, here, and never stored nor logged: InviteStore keeps
only its SHA-256; audit_log records that an invitation was issued.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import AuditAction, User
from app.web.deps import DB, USER, WebUser, is_family_member, require_family_member
from app.web.invites import InviteLimit, MAX_ACTIVE_PER_MEMBER
from app.web.render import audit, hx_trigger, render

logger = logging.getLogger(__name__)
router = APIRouter()


def can_manage_users(db: Session, user: WebUser) -> bool:
    """Family members only: the demo account must not see family names."""
    return is_family_member(db, user)


def family_members(db: Session) -> List[User]:
    # The users table itself: listing it is the point (family members only).
    return db.query(User).order_by(func.lower(User.username)).all()


@router.post("/mas/invitaciones")
def invite_issue(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    require_family_member(db, user)
    try:
        code, invite = request.app.state.invites.issue(user.id, user.username)
    except InviteLimit:
        return render(request, "partials/invite_limit.html", {"max_active": MAX_ACTIVE_PER_MEMBER}, user=user)
    expires = datetime.fromtimestamp(invite.expires)
    audit(db, request, user, "invitations", None, AuditAction.insert,
          new=f"invitación emitida, caduca a las {expires:%H:%M}")
    logger.info("Web: '%s' emitió una invitación (caduca %s)", user.username, f"{expires:%H:%M}")
    response = render(request, "partials/invite_issued.html", {
        "code": code, "expires_ms": int(invite.expires * 1000), "expires": expires,
        "minutes": round((invite.expires - invite.created) / 60),
    }, user=user)
    hx_trigger(response, refreshPage=True)      # the list of active invitations behind the sheet
    return response


@router.post("/mas/invitaciones/{invite_id}/anular")
def invite_revoke(request: Request, invite_id: str, user: WebUser = USER, db: Session = DB) -> Response:
    require_family_member(db, user)
    if not request.app.state.invites.revoke(invite_id, user.id):
        raise HTTPException(status_code=404)
    audit(db, request, user, "invitations", None, AuditAction.delete, old="invitación anulada")
    response = Response(status_code=204)
    hx_trigger(response, toast="Invitación anulada", refreshPage=True)
    return response
