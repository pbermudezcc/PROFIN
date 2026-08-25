"""
app/auth/auth_service.py
=========================
PROFIN — Authentication Service

Provides three public functions:
  create_user(session, username, password) -> User
  verify_login(session, username, password) -> User | None
  get_user_by_id(session, user_id) -> User | None

Password hashing uses bcrypt directly (rounds=12).
passlib dependency removed in v0.0.3 due to bcrypt >= 4.x incompatibility.

Changelog:
  v0.0.3  Replaced passlib with direct bcrypt calls.
          Removed is_active field (not in User model v0.0.2).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hash_password(plain: str) -> str:
    """
    Hash a plaintext password using bcrypt (rounds=12).
    Returns a UTF-8 decoded hash string for storage in the DB.
    """
    return bcrypt.hashpw(
        plain.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    ).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    """
    Verify a plaintext password against a stored bcrypt hash.
    Returns True if the password matches, False otherwise.
    Safe against all bcrypt exceptions.
    """
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8"),
            hashed.encode("utf-8"),
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_user(session: Session, username: str, password: str) -> User:
    """
    Create and persist a new User with a hashed password.

    Parameters
    ----------
    session  : Active SQLAlchemy session.
    username : Must be non-empty string. Uniqueness enforced at DB level.
    password : Must be non-empty string. Stored as bcrypt hash only.

    Returns
    -------
    User ORM object (already committed to the session).

    Raises
    ------
    ValueError : If username or password is empty.
    ValueError : If username already exists (duplicate).
    """
    if not username or not username.strip():
        raise ValueError("El nombre de usuario no puede estar vacío.")
    if not password:
        raise ValueError("La contraseña no puede estar vacía.")

    username      = username.strip()
    password_hash = _hash_password(password)

    user = User(
        username      = username,
        password_hash = password_hash,
        created_at    = datetime.now(timezone.utc),
    )
    session.add(user)

    try:
        session.commit()
        session.refresh(user)
        logger.info("Usuario creado: '%s' (id=%s)", username, user.id)
        return user

    except IntegrityError:
        session.rollback()
        raise ValueError(
            f"El usuario '{username}' ya existe. "
            "Elige un nombre de usuario diferente."
        )


def verify_login(
    session:  Session,
    username: str,
    password: str,
) -> Optional[User]:
    """
    Verify login credentials and update last_login on success.

    Returns
    -------
    User object if credentials are valid, None otherwise.
    """
    if not username or not password:
        return None

    user: Optional[User] = (
        session.query(User)
        .filter(User.username == username.strip())
        .first()
    )

    if user is None:
        logger.debug("Login fallido: usuario '%s' no encontrado.", username)
        return None

    if not _verify_password(password, user.password_hash):
        logger.debug("Login fallido: contraseña incorrecta para '%s'.", username)
        return None

    # Update last_login
    user.last_login = datetime.now(timezone.utc)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            "No se pudo actualizar last_login para '%s': %s", username, exc
        )

    logger.info("Login exitoso: '%s' (id=%s)", username, user.id)
    return user


def get_user_by_id(session: Session, user_id: int) -> Optional[User]:
    """Retrieve a user by primary key. Returns None if not found."""
    return session.get(User, user_id)