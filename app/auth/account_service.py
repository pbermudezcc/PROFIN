"""
app/auth/account_service.py
===========================
FAMILYFIN — A member manages their OWN account (Qt-free, shared by the
desktop and the web «Mi cuenta»):

  change_password(session, user_id, current, new, confirm)
  rename_user(session, user_id, password, new_name) -> (old, new)
  deletion_summary(session, user_id) -> DeletionSummary
  delete_user(session, user_id, password, confirm_name, backup, source) -> Path
  verify_password(session, user_id, password)  (re-authentication only)

Every operation asks for the current password. There is no admin role:
callers must pass the id of the signed-in user, never one taken from a form.
Errors are AccountError(field, Spanish message).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.account_rules import AccountError, is_demo_name, validate_password, validate_username
from app.auth.auth_service import hash_password, password_matches
from app.db.models import (
    Account,
    AuditAction,
    AuditLog,
    AutoCategorizationRule,
    Category,
    ImportBatch,
    InvestmentTransaction,
    Transaction,
    User,
)

logger = logging.getLogger(__name__)

WRONG_PASSWORD = "La contraseña no es correcta."


class WrongPassword(AccountError):
    """The password given to confirm the operation is not the user's.
    Callers that face the network count it in their login throttle."""


def _user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise AccountError(None, "La cuenta ya no existe.")
    return user


def _check_password(user: User, password: Optional[str], field: str = "password") -> None:
    if not password_matches(user, password):
        raise WrongPassword(field, WRONG_PASSWORD)


def verify_password(session: Session, user_id: int, password: str) -> None:
    """Re-authentication before a sensitive read (e.g. «Descargar mis datos»).
    Raises WrongPassword (field "password")."""
    _check_password(_user(session, user_id), password)


# ---------------------------------------------------------------------------
# Password and name
# ---------------------------------------------------------------------------

def change_password(session: Session, user_id: int, current: str, new: str, confirm: str) -> None:
    """Set a new password after checking the current one (field "current")."""
    user = _user(session, user_id)
    _check_password(user, current, field="current")
    validate_password(new, confirm, user.username)
    if new == current:
        raise AccountError("password", "La nueva contraseña debe ser distinta de la actual.")
    user.password_hash = hash_password(new)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info("Contraseña cambiada: '%s' (id=%s)", user.username, user.id)


def rename_user(session: Session, user_id: int, password: str, new_name: str) -> Tuple[str, str]:
    """Rename the account after checking its password. Returns (old, new)."""
    user = _user(session, user_id)
    if is_demo_name(user.username):
        raise AccountError("username", "La cuenta de demostración no se puede renombrar.")
    name = validate_username(session, new_name, exclude_id=user.id)
    if name == user.username:
        raise AccountError("username", "Ese ya es tu nombre de usuario.")
    _check_password(user, password)
    old = user.username
    user.username = name
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise AccountError("username", f"El usuario «{name}» ya existe. Elige otro nombre.")
    logger.info("Usuario renombrado: '%s' -> '%s' (id=%s)", old, name, user.id)
    return old, name


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeletionSummary:
    accounts: int
    transactions: int
    investment_operations: int
    categories: int
    rules: int
    imports: int
    global_rules: int          # family rules (user_id NULL) that point at this user's categories

    @property
    def blocked(self) -> bool:
        return self.global_rules > 0


def deletion_summary(session: Session, user_id: int) -> DeletionSummary:
    def count(model, *criteria) -> int:
        return session.query(func.count(model.id)).filter(*criteria).scalar() or 0

    own_categories = session.query(Category.id).filter(Category.user_id == user_id)
    return DeletionSummary(
        accounts=count(Account, Account.user_id == user_id),
        transactions=count(Transaction, Transaction.user_id == user_id),
        investment_operations=count(InvestmentTransaction, InvestmentTransaction.user_id == user_id),
        categories=count(Category, Category.user_id == user_id),
        rules=count(AutoCategorizationRule, AutoCategorizationRule.user_id == user_id),
        imports=count(ImportBatch, ImportBatch.user_id == user_id),
        global_rules=count(
            AutoCategorizationRule,
            AutoCategorizationRule.user_id.is_(None),
            AutoCategorizationRule.category_id.in_(own_categories),
        ),
    )


def delete_user(
    session: Session,
    user_id: int,
    password: str,
    confirm_name: str,
    backup: Callable[[], Path],
    source: str = "web",
) -> Path:
    """Delete the account and all its data. Returns the backup file.

    Order: exact name, password, no family rule depends on its categories
    (deleting them would cascade into rules the whole family uses), then the
    backup (if it fails nothing is deleted), then the ORM cascade. An
    audit_log row with user_id NULL records who was deleted and where the
    copy is; it survives the cascade that removes the user's own rows.
    """
    user = _user(session, user_id)
    if (confirm_name or "").strip() != user.username:
        raise AccountError("confirm_name", "El nombre escrito no coincide con tu usuario.")
    _check_password(user, password)

    summary = deletion_summary(session, user_id)
    if summary.blocked:
        raise AccountError(None, (
            f"{summary.global_rules} reglas globales de la familia usan tus categorías. "
            "Muévelas o bórralas desde el PC (Reglas) antes de borrar tu cuenta."
        ))

    try:
        backup_path = Path(backup())
    except Exception:
        logger.exception("No se pudo hacer la copia antes de borrar la cuenta '%s'", user.username)
        raise AccountError(None, (
            "No se pudo hacer la copia de seguridad, así que no se ha borrado nada. "
            "Revisa la carpeta de copias en el PC e inténtalo de nuevo."
        ))

    name, uid = user.username, user.id
    try:
        session.add(AuditLog(
            user_id=None,
            table_affected="users",
            record_id=uid,
            action=AuditAction.delete,
            old_value=(f"usuario «{name}» (id {uid}): {summary.accounts} cuentas, "
                       f"{summary.transactions} movimientos, {summary.investment_operations} operaciones"),
            new_value=f"cuenta borrada desde {source}; copia previa {backup_path.name}",
        ))
        # Other members' traspaso categories that point at this user lose the
        # link (explicit too: SQLite only applies ON DELETE with foreign_keys on).
        session.query(Category).filter(Category.transfer_peer_user_id == uid).update(
            {Category.transfer_peer_user_id: None}, synchronize_session=False)
        session.delete(user)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("No se pudo borrar la cuenta '%s'", name)
        raise AccountError(None, "No se pudo borrar la cuenta. No se ha cambiado nada.")
    logger.info("Cuenta borrada desde %s: '%s' (id=%s), copia %s", source, name, uid, backup_path)
    return backup_path
