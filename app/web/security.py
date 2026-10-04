"""
app/web/security.py
===================
FAMILYFIN — Sessions, login throttling and request hardening for the LAN
web server.

- Sessions are server-side (in memory): the cookie only carries a random
  256-bit id. Restarting the app or switching the data folder logs every
  device out, which is the safe default.
- Every state-changing request must come from our own origin (Origin or
  Referer) and, once logged in, carry the session's CSRF token.
- Only private-network / loopback clients are served.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.web.tls import is_lan_address

logger = logging.getLogger(__name__)

SESSION_COOKIE = "__Host-ffsid"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
IDLE_TIMEOUT = 30 * 60
ABSOLUTE_TIMEOUT = 12 * 3600
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Request body caps: forms are tiny; only statement uploads are bigger.
MAX_BODY = 256 * 1024
MAX_UPLOAD_BODY = 6 * 1024 * 1024
UPLOAD_PATHS = ("/importar",)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclass
class WebSession:
    sid: str
    user_id: int
    username: str
    csrf: str
    ip: str
    user_agent: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, idle: int = IDLE_TIMEOUT, absolute: int = ABSOLUTE_TIMEOUT, clock=time.time):
        self._sessions: Dict[str, WebSession] = {}
        self._lock = threading.Lock()
        self._idle = idle
        self._absolute = absolute
        self._clock = clock

    def create(self, user_id: int, username: str, ip: str, user_agent: str) -> WebSession:
        now = self._clock()
        session = WebSession(
            sid=secrets.token_urlsafe(32),
            user_id=user_id,
            username=username,
            csrf=secrets.token_urlsafe(32),
            ip=ip,
            user_agent=user_agent[:200],
            created=now,
            last_seen=now,
        )
        with self._lock:
            self._sessions[session.sid] = session
        return session

    def get(self, sid: Optional[str]) -> Optional[WebSession]:
        if not sid:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if now - session.last_seen > self._idle or now - session.created > self._absolute:
                del self._sessions[sid]
                return None
            session.last_seen = now
            return session

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def revoke_user(self, user_id: int) -> int:
        with self._lock:
            sids = [s for s, v in self._sessions.items() if v.user_id == user_id]
            for sid in sids:
                del self._sessions[sid]
            return len(sids)

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def active(self) -> List[WebSession]:
        now = self._clock()
        with self._lock:
            for sid in [
                s for s, v in self._sessions.items()
                if now - v.last_seen > self._idle or now - v.created > self._absolute
            ]:
                del self._sessions[sid]
            return sorted(self._sessions.values(), key=lambda s: s.last_seen, reverse=True)


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------

class LoginLimiter:
    """Exponential backoff per client IP and per username.

    After FREE failures each further failure doubles the wait (1, 2, 4… s,
    capped at MAX_DELAY); at LOCK_AFTER failures the key is locked for
    LOCK_SECONDS. A success clears the username and the IP.
    """

    FREE = 3
    MAX_DELAY = 60
    LOCK_AFTER = 10
    LOCK_SECONDS = 15 * 60

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._state: Dict[str, Tuple[int, float]] = {}  # key -> (failures, blocked_until)

    @staticmethod
    def _keys(ip: str, username: str) -> List[str]:
        return [f"ip:{ip}", f"user:{username.strip().lower()}"]

    def retry_after(self, ip: str, username: str) -> int:
        now = self._clock()
        with self._lock:
            wait = max(
                (self._state.get(k, (0, 0.0))[1] - now for k in self._keys(ip, username)),
                default=0,
            )
        return max(0, int(wait + 0.999))

    def failure(self, ip: str, username: str) -> None:
        now = self._clock()
        with self._lock:
            for key in self._keys(ip, username):
                failures, _ = self._state.get(key, (0, 0.0))
                failures += 1
                if failures >= self.LOCK_AFTER:
                    blocked = now + self.LOCK_SECONDS
                elif failures > self.FREE:
                    blocked = now + min(self.MAX_DELAY, 2 ** (failures - self.FREE - 1))
                else:
                    blocked = 0.0
                self._state[key] = (failures, blocked)

    def success(self, ip: str, username: str) -> None:
        with self._lock:
            for key in self._keys(ip, username):
                self._state.pop(key, None)


def tokens_match(expected: str, received: Optional[str]) -> bool:
    return bool(received) and hmac.compare_digest(expected.encode(), received.encode())


# ---------------------------------------------------------------------------
# ASGI middleware: LAN-only clients, same-origin writes, security headers
# ---------------------------------------------------------------------------

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "manifest-src 'self'; "
    "worker-src 'self'"
)

SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]


def _same_origin(headers: Headers, scheme: str) -> bool:
    host = headers.get("host", "")
    # Some browsers send "Origin: null" on same-site form posts under strict
    # referrer policies; the Referer (same-origin policy) then identifies it.
    origin = headers.get("origin")
    source = origin if origin and origin != "null" else headers.get("referer")
    if not source:
        return False
    parts = urlsplit(source)
    return parts.scheme == scheme and parts.netloc == host


def _capped_receive(receive: Receive, limit: int) -> Receive:
    """Stop reading a body (chunked or lying Content-Length) past limit:
    the app sees a client disconnect and abandons the request."""
    received = 0

    async def wrapped() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                logger.warning("Web: cuerpo de petición demasiado grande, cortado")
                return {"type": "http.disconnect"}
        return message

    return wrapped


class HardeningMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware): cheap and streaming-safe."""

    def __init__(self, app: ASGIApp, lan_only: bool = True):
        self.app = app
        self.lan_only = lan_only

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        if self.lan_only and (client is None or not is_lan_address(client[0])):
            logger.warning("Web: rechazado cliente fuera de la red local (%s)", client[0] if client else "?")
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if scope["method"] not in SAFE_METHODS and not _same_origin(headers, scope.get("scheme", "https")):
            logger.warning(
                "Web: %s %s rechazado por origen (host=%s origin=%s referer=%s)",
                scope["method"], scope.get("path"), headers.get("host"),
                headers.get("origin"), headers.get("referer"),
            )
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return

        path = scope.get("path", "")
        is_static = path.startswith("/static/")

        if scope["method"] not in SAFE_METHODS:
            limit = MAX_UPLOAD_BODY if path.startswith(UPLOAD_PATHS) else MAX_BODY
            declared = headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                logger.warning("Web: %s %s rechazado por tamaño (%s bytes)", scope["method"], path, declared)
                await PlainTextResponse("Payload Too Large", status_code=413)(scope, receive, send)
                return
            receive = _capped_receive(receive, limit)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                existing = {name.lower() for name, _ in raw}
                for name, value in SECURITY_HEADERS:
                    if name not in existing:
                        raw.append((name, value))
                if not is_static and b"cache-control" not in existing:
                    raw.append((b"cache-control", b"no-store"))
                raw = [(n, v) for n, v in raw if n.lower() != b"server"]
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_headers)
