"""
In-process session manager for PROFIN.

Holds the currently authenticated user for the lifetime of the application.
A new login clears any previous session. Session is cleared on app exit.

This is intentionally simple (single-user, single-process) — no tokens,
no persistence between restarts.
"""

from __future__ import annotations

from typing import Optional
from app.db.models import User


class SessionManager:
    """Singleton-style in-memory session store."""

    _current_user: Optional[User] = None

    @classmethod
    def login(cls, user: User) -> None:
        """Set the active session user."""
        cls._current_user = user

    @classmethod
    def logout(cls) -> None:
        """Clear the active session."""
        cls._current_user = None

    @classmethod
    def get_current_user(cls) -> Optional[User]:
        """Return the currently logged-in user, or None."""
        return cls._current_user

    @classmethod
    def is_authenticated(cls) -> bool:
        """Return True if a user is currently logged in."""
        return cls._current_user is not None

    @classmethod
    def require_user(cls) -> User:
        """
        Return the current user or raise RuntimeError.
        Use in GUI handlers where a logged-in user is always expected.
        """
        if cls._current_user is None:
            raise RuntimeError(
                "No hay ningún usuario autenticado. "
                "Por favor, inicia sesión antes de continuar."
            )
        return cls._current_user