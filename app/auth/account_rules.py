"""
app/auth/account_rules.py
=========================
FAMILYFIN — Rules for user names and passwords, shared by the desktop
dialogs and the web (registration by invitation, «Mi cuenta»).

All messages are Spanish and attached to a form field through
AccountError(field, message), so every front-end can show them next to the
right input.

Names
  * surrounding spaces trimmed, Unicode NFC, 2–32 characters;
  * letters (accents included), digits, space, «.», «_» and «-» only;
  * the demo account name (config.DEMO_USERNAME) is reserved in any case:
    the rules engine keeps that account apart from the family's global rules;
  * unique ignoring case AND accents («Lucía» clashes with «lucia»). The
    UNIQUE index on users.username stays the final safety net.

Passwords (NIST SP 800-63B: length and a deny-list, no composition rules)
  * new passwords: 8–64 characters and at most 72 UTF-8 bytes (bcrypt only
    uses 72 bytes; bcrypt >= 5 refuses longer input). Existing shorter
    passwords keep working: the rules apply only when a password is set;
  * not equal to the user name, not in common_passwords.txt;
  * the confirmation must match.
"""
from __future__ import annotations

import logging
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.config import DEMO_USERNAME
from app.db.models import User

logger = logging.getLogger(__name__)

MIN_USERNAME_LENGTH = 2
MAX_USERNAME_LENGTH = 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 64
MAX_PASSWORD_BYTES = 72          # bcrypt limit
USERNAME_SYMBOLS = " ._-"

PASSWORD_TOO_LONG = (
    "La contraseña es demasiado larga para guardarla de forma segura "
    "(las tildes y los emojis ocupan más); acórtala."
)
COMMON_PASSWORDS_FILE = Path(__file__).with_name("common_passwords.txt")


class AccountError(ValueError):
    """A rule failed. `field` names the form input it belongs to
    ("username", "password", "confirm", "current", "confirm_name") or is
    None for errors about the whole operation."""

    def __init__(self, field: Optional[str], message: str):
        super().__init__(message)
        self.field = field
        self.message = message


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def normalize_username(name: Optional[str]) -> str:
    """Trimmed NFC form: the way names are stored and looked up."""
    return unicodedata.normalize("NFC", (name or "").strip())


def name_key(name: str) -> str:
    """Comparison key ignoring case and accents («Lucía» == «LUCIA»)."""
    decomposed = unicodedata.normalize("NFKD", normalize_username(name).casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def is_demo_name(name: str) -> bool:
    return name_key(name) == name_key(DEMO_USERNAME)


def validate_username(session: Session, name: Optional[str], exclude_id: Optional[int] = None) -> str:
    """Return the normalized name or raise AccountError("username", …)."""
    value = normalize_username(name)
    if not value:
        raise AccountError("username", "Escribe un nombre de usuario.")
    if len(value) < MIN_USERNAME_LENGTH:
        raise AccountError("username", f"El nombre debe tener al menos {MIN_USERNAME_LENGTH} caracteres.")
    if len(value) > MAX_USERNAME_LENGTH:
        raise AccountError("username", f"El nombre puede tener como máximo {MAX_USERNAME_LENGTH} caracteres.")
    if any(not (ch.isalpha() or ch.isdigit() or ch in USERNAME_SYMBOLS) for ch in value):
        raise AccountError("username", "Usa solo letras, números, espacios, puntos, guiones o guiones bajos.")
    if is_demo_name(value):
        raise AccountError("username", f"«{DEMO_USERNAME}» está reservado para la cuenta de demostración.")
    wanted = name_key(value)
    for user_id, existing in session.query(User.id, User.username):
        if user_id != exclude_id and name_key(existing) == wanted:
            raise AccountError("username", f"Ya existe el usuario «{existing}». Elige otro nombre.")
    return value


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _common_passwords() -> frozenset:
    try:
        lines = COMMON_PASSWORDS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.error("No se encontró la lista de contraseñas comunes (%s)", COMMON_PASSWORDS_FILE)
        return frozenset()
    return frozenset(line.strip().casefold() for line in lines if line.strip() and not line.startswith("#"))


def is_common_password(password: str) -> bool:
    return password.casefold() in _common_passwords()


def password_errors(password: Optional[str], confirm: Optional[str], username: str = "") -> Dict[str, str]:
    """Errors for a NEW password: {"password": …, "confirm": …} (empty = ok)."""
    errors: Dict[str, str] = {}
    password = password or ""
    if not password:
        errors["password"] = "Escribe una contraseña."
    elif len(password) < MIN_PASSWORD_LENGTH:
        errors["password"] = f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres."
    elif len(password) > MAX_PASSWORD_LENGTH:
        errors["password"] = f"La contraseña puede tener como máximo {MAX_PASSWORD_LENGTH} caracteres."
    elif len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        errors["password"] = PASSWORD_TOO_LONG
    elif username and name_key(password) == name_key(username):
        errors["password"] = "La contraseña no puede ser tu nombre de usuario."
    elif is_common_password(password):
        errors["password"] = "Es una contraseña demasiado común. Elige otra menos previsible."
    if password and (confirm or "") != password:
        errors["confirm"] = "Las contraseñas no coinciden."
    return errors


def validate_password(password: Optional[str], confirm: Optional[str], username: str = "") -> None:
    """Raise AccountError for the first broken password rule."""
    errors = password_errors(password, confirm, username)
    for field in ("password", "confirm"):
        if field in errors:
            raise AccountError(field, errors[field])


def validate_new_user(session: Session, username: Optional[str], password: Optional[str],
                      confirm: Optional[str]) -> Dict[str, str]:
    """All errors of a new account at once, by field (empty dict = valid).
    Used by forms that show every problem together."""
    errors: Dict[str, str] = {}
    try:
        name = validate_username(session, username)
    except AccountError as exc:
        errors["username"] = exc.message
        name = normalize_username(username)
    errors.update(password_errors(password, confirm, name))
    return errors
