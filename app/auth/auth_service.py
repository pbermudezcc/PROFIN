"""
app/auth/auth_service.py
=========================
FAMILYFIN — Authentication Service

Public functions:
  create_user(session, username, password) -> User
  verify_login(session, username, password) -> User | None
  get_user_by_id(session, user_id) -> User | None
  password_matches(user, password) -> bool
  hash_password(plain) -> str

Name and password rules live in app/auth/account_rules.py (re-exported
here); changing the password, renaming and deleting an account live in
app/auth/account_service.py.

Password hashing uses bcrypt directly (rounds=12).
passlib dependency removed in v0.0.3 due to bcrypt >= 4.x incompatibility.

Changelog:
  v0.0.3  Replaced passlib with direct bcrypt calls.
          Removed is_active field (not in User model v0.0.2).
  W10     create_user applies the shared name/password rules; verify_login
          spends the same bcrypt time when the user does not exist.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.account_rules import (  # noqa: F401  (re-exported for callers)
    MAX_PASSWORD_BYTES,
    MAX_PASSWORD_LENGTH,
    MAX_USERNAME_LENGTH,
    MIN_PASSWORD_LENGTH,
    MIN_USERNAME_LENGTH,
    PASSWORD_TOO_LONG,
    AccountError,
    normalize_username,
    validate_new_user,
    validate_password,
    validate_username,
)
from app.db.models import User

logger = logging.getLogger(__name__)

# Checked when the user does not exist, so an unknown name costs the same
# bcrypt time as a wrong password (no way to tell which names exist).
# A hash of a random string nobody knows (rounds=12, like real hashes).
_DUMMY_HASH = "$2b$12$4975Qpb7OGk2vWkD5uSC2.Jy3MS0WTrLBRZ.OSLYQbMIwDcyqHqta"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """
    Hash a plaintext password using bcrypt (rounds=12).
    Returns a UTF-8 decoded hash string for storage in the DB.
    """
    if len(plain.encode("utf-8")) > MAX_PASSWORD_BYTES:
        # bcrypt 5 raises an English ValueError here; never show that in the GUI.
        raise AccountError("password", PASSWORD_TOO_LONG)
    return bcrypt.hashpw(
        plain.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    ).decode("utf-8")


_hash_password = hash_password      # backwards-compatible private name


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


def password_matches(user: User, password: Optional[str]) -> bool:
    """True when `password` is the user's current password."""
    return bool(password) and _verify_password(password, user.password_hash)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_user(
    session:  Session,
    username: str,
    password: str,
    *,
    enforce_policy: bool = True,
) -> User:
    """
    Create and persist a new User with a hashed password.

    The name is stored trimmed and in Unicode NFC. With enforce_policy (the
    default, used by every screen) the shared rules of account_rules apply:
    name 2–32 characters, unique ignoring case and accents, «DEMO»
    reserved; password 8–64 characters, not common, not the name.
    enforce_policy=False is only for trusted tooling that creates fixed
    accounts (tools/demo, tools/web_preview, test fixtures): it still
    refuses empty values and passwords over bcrypt's 72-byte limit.

    Raises
    ------
    AccountError (a ValueError) with the offending field and a Spanish
    message; also when the name already exists at the database level.
    """
    if enforce_policy:
        name = validate_username(session, username)
        validate_password(password, password, name)
    else:
        name = normalize_username(username)
        if not name:
            raise AccountError("username", "El nombre de usuario no puede estar vacío.")
        if not password:
            raise AccountError("password", "La contraseña no puede estar vacía.")

    user = User(
        username      = name,
        password_hash = hash_password(password),
        created_at    = datetime.now(timezone.utc),
    )
    session.add(user)

    try:
        session.commit()
        session.refresh(user)
        logger.info("Usuario creado: '%s' (id=%s)", name, user.id)
        return user

    except IntegrityError:
        session.rollback()
        raise AccountError(
            "username",
            f"El usuario «{name}» ya existe. Elige un nombre de usuario diferente.",
        )


def verify_login(
    session:  Session,
    username: str,
    password: str,
) -> Optional[User]:
    """
    Verify login credentials and update last_login on success.

    An unknown user name still runs one bcrypt check (against a dummy
    hash), so the response time does not reveal which names exist.

    Returns
    -------
    User object if credentials are valid, None otherwise.
    """
    if not username or not password:
        return None

    typed = username.strip()
    candidates = {typed, normalize_username(typed)}
    user: Optional[User] = (
        session.query(User)
        .filter(User.username.in_(candidates))
        .order_by(User.id)
        .first()
    )

    if user is None:
        _verify_password(password, _DUMMY_HASH)
        logger.debug("Login fallido: usuario no encontrado.")
        return None

    if not _verify_password(password, user.password_hash):
        logger.debug("Login fallido: contraseña incorrecta para '%s'.", user.username)
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

    logger.info("Login exitoso: '%s' (id=%s)", user.username, user.id)
    return user


def get_user_by_id(session: Session, user_id: int) -> Optional[User]:
    """Retrieve a user by primary key. Returns None if not found."""
    return session.get(User, user_id)
