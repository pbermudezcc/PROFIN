"""
app/web/invites.py
==================
FAMILYFIN — Invitation codes to join the family from the web (W10).

A signed-in member issues a code; the new person types it on the login
page (/registro) and creates their own user. Like the web sessions, the
store lives in memory only: restarting the app, switching the data folder
or regenerating the certificates drops every pending invitation.

- Code: 8 characters from a 30-symbol alphabet (Crockford base32 without
  0/O and 1/I/L, so nothing is ambiguous when read aloud or typed on a
  phone), shown as XXXX-XXXX: about 39 bits from `secrets`. Typing it in
  lowercase, without the hyphen or with spaces is accepted.
- Only the SHA-256 of the code is kept, never the code itself.
- Single use, 15 minutes, at most 3 active invitations per member.
- consume() is atomic (lock): two people with the same code → one account.
  restore() puts it back if the account could not be created after all.
- Guessing is throttled by the router (LoginLimiter); this store only
  answers "valid or not".
"""
from __future__ import annotations

import hashlib
import secrets
import string
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
TTL_SECONDS = 15 * 60
MAX_ACTIVE_PER_MEMBER = 3


class InviteLimit(Exception):
    """The member already has MAX_ACTIVE_PER_MEMBER active invitations."""


@dataclass
class Invite:
    id: str                  # public handle for «Anular»; unrelated to the code
    digest: str              # SHA-256 of the normalized code
    created_by: int
    created_by_name: str
    created: float
    expires: float

    def remaining(self, now: float) -> int:
        return max(0, int(self.expires - now))


def normalize_code(text: Optional[str]) -> str:
    """'abcd-efgh', 'ABCD EFGH' and 'abcdefgh' are the same code."""
    return "".join(ch for ch in (text or "").upper() if ch in string.ascii_uppercase + string.digits)


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _digest(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("ascii", "ignore")).hexdigest()


class InviteStore:
    def __init__(self, ttl: int = TTL_SECONDS, max_active: int = MAX_ACTIVE_PER_MEMBER, clock=time.time):
        self._ttl = ttl
        self._max_active = max_active
        self._clock = clock
        self._lock = threading.Lock()
        self._by_digest: Dict[str, Invite] = {}

    # ── internal ─────────────────────────────────────────────────────
    def _purge(self, now: float) -> None:
        for digest in [d for d, inv in self._by_digest.items() if inv.expires <= now]:
            del self._by_digest[digest]

    def _lookup(self, code: str, now: float) -> Optional[Invite]:
        normalized = normalize_code(code)
        if len(normalized) != CODE_LENGTH:
            return None
        invite = self._by_digest.get(_digest(normalized))
        if invite is None or invite.expires <= now:
            return None
        return invite

    # ── API ──────────────────────────────────────────────────────────
    def issue(self, user_id: int, username: str) -> Tuple[str, Invite]:
        """New invitation from a member. Returns (code as XXXX-XXXX, invite).
        The plain code is only in this return value: show it once."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            if sum(1 for inv in self._by_digest.values() if inv.created_by == user_id) >= self._max_active:
                raise InviteLimit()
            while True:
                code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
                digest = _digest(code)
                if digest not in self._by_digest:
                    break
            invite = Invite(id=secrets.token_urlsafe(9), digest=digest, created_by=user_id,
                            created_by_name=username, created=now, expires=now + self._ttl)
            self._by_digest[digest] = invite
            return format_code(code), invite

    def peek(self, code: str) -> Optional[Invite]:
        """The invitation behind a code if it is still valid (not used)."""
        with self._lock:
            return self._lookup(code, self._clock())

    def consume(self, code: str) -> Optional[Invite]:
        """Take the invitation (single use). None if invalid, expired or
        already taken by someone else a moment ago."""
        now = self._clock()
        with self._lock:
            invite = self._lookup(code, now)
            if invite is not None:
                del self._by_digest[invite.digest]
            return invite

    def restore(self, invite: Invite) -> None:
        """Give back a consumed invitation whose account could not be created."""
        with self._lock:
            if invite.expires > self._clock():
                self._by_digest.setdefault(invite.digest, invite)

    def revoke(self, invite_id: str, user_id: int) -> bool:
        """«Anular»: a member can only cancel their own invitations."""
        with self._lock:
            for digest, invite in list(self._by_digest.items()):
                if invite.id == invite_id and invite.created_by == user_id:
                    del self._by_digest[digest]
                    return True
            return False

    def revoke_by(self, user_id: int) -> int:
        """Every invitation of a member (their account is deleted)."""
        with self._lock:
            digests = [d for d, inv in self._by_digest.items() if inv.created_by == user_id]
            for digest in digests:
                del self._by_digest[digest]
            return len(digests)

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._by_digest)
            self._by_digest.clear()
            return count

    def active_by(self, user_id: int) -> List[Invite]:
        now = self._clock()
        with self._lock:
            self._purge(now)
            return sorted((inv for inv in self._by_digest.values() if inv.created_by == user_id),
                          key=lambda inv: inv.created)

    def rename_member(self, user_id: int, username: str) -> None:
        """Keep «Te invita …» right after a member renames themselves."""
        with self._lock:
            for invite in self._by_digest.values():
                if invite.created_by == user_id:
                    invite.created_by_name = username
