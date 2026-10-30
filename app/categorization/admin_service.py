"""
app/categorization/admin_service.py
===================================
FAMILYFIN — Category and rule administration for one logged-in user.

Qt-free and shared by the desktop views (CategoriesView, RulesView) and the
web front-end. Every function takes the acting user's id and refuses to
touch another family member's categories or rules: the lower-level CRUD in
category_manager / rules_engine works on bare ids, which is fine inside the
desktop app but not for ids that arrive from a browser.

Global rules (user_id NULL) are shared by the whole family; they can be
read here but only created/changed from the desktop app.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.categorization.category_manager import (
    create_category,
    delete_category,
    seed_default_categories,
    seed_default_rules,
    update_category_name,
)
from app.categorization.rules_engine import (
    RulesEngineResult,
    apply_rules,
    apply_rules_and_commit,
    create_rule,
    delete_rule,
    rule_scope,
    update_rule,
)
from app.db.models import (
    CATEGORY_SOURCE_MANUAL,
    AutoCategorizationRule,
    Category,
    CategoryLevel,
    MatchType,
    Transaction,
    TRANSACTIONS_NEWEST_FIRST,
)

logger = logging.getLogger(__name__)

MAX_NAME = 80
MAX_PATTERN = 200


class AdminError(ValueError):
    """A validation problem to show to the user as is."""


class NotFound(LookupError):
    """The record does not exist or belongs to someone else."""


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def own_category(session: Session, user_id: int, category_id: int) -> Category:
    cat = session.get(Category, category_id)
    if cat is None or cat.user_id != user_id:
        raise NotFound("Categoría no encontrada.")
    return cat


def _clean_name(name: Optional[str]) -> str:
    name = (name or "").strip()
    if not name:
        raise AdminError("El nombre no puede estar vacío.")
    if len(name) > MAX_NAME:
        raise AdminError(f"El nombre no puede superar {MAX_NAME} caracteres.")
    return name


def _check_unique(session: Session, user_id: int, parent_id: Optional[int], name: str,
                  exclude_id: Optional[int] = None) -> None:
    query = session.query(Category.id).filter(
        Category.user_id == user_id,
        Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id,
        func.lower(Category.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.filter(Category.id != exclude_id)
    if query.first() is not None:
        raise AdminError(f"Ya existe «{name}» en ese grupo.")


def add_category(session: Session, user_id: int, parent_id: int, name: str) -> Category:
    """New category under one of the user's type nodes (GASTO, INGRESO…)."""
    parent = own_category(session, user_id, parent_id)
    if parent.level != CategoryLevel.type:
        raise AdminError("Las categorías se crean dentro de un tipo (GASTO, INGRESO…).")
    name = _clean_name(name)
    _check_unique(session, user_id, parent.id, name)
    return create_category(session, user_id, name, CategoryLevel.category, parent_id=parent.id)


def rename_category(session: Session, user_id: int, category_id: int, name: str) -> Category:
    cat = own_category(session, user_id, category_id)
    name = _clean_name(name)
    _check_unique(session, user_id, cat.parent_id, name, exclude_id=cat.id)
    return update_category_name(session, cat.id, name)


@dataclass
class CategoryUsage:
    transactions: int = 0
    manual: int = 0
    rules: int = 0


def category_usage(session: Session, user_id: int, category_id: int) -> CategoryUsage:
    """What deleting this category would affect (shown before confirming)."""
    own_category(session, user_id, category_id)
    usage = CategoryUsage()
    usage.transactions = session.query(func.count(Transaction.id)).filter(
        Transaction.user_id == user_id, Transaction.category_id == category_id).scalar() or 0
    usage.manual = session.query(func.count(Transaction.id)).filter(
        Transaction.user_id == user_id, Transaction.category_id == category_id,
        Transaction.category_source == CATEGORY_SOURCE_MANUAL).scalar() or 0
    usage.rules = session.query(func.count(AutoCategorizationRule.id)).filter(
        AutoCategorizationRule.category_id == category_id).scalar() or 0
    return usage


def remove_category(session: Session, user_id: int, category_id: int) -> CategoryUsage:
    """Delete a category (not a type node). Its transactions become
    uncategorized and go back to the rules; rules pointing at it are deleted
    with it (FK cascade), exactly as on the desktop."""
    cat = own_category(session, user_id, category_id)
    if cat.level == CategoryLevel.type:
        raise AdminError("Los tipos (GASTO, INGRESO…) no se pueden borrar.")
    usage = category_usage(session, user_id, category_id)
    # Done explicitly too: SQLite only cascades with PRAGMA foreign_keys on,
    # and a NULL category with category_source='manual' would stay locked.
    session.query(Transaction).filter(
        Transaction.user_id == user_id, Transaction.category_id == category_id,
    ).update({Transaction.category_id: None, Transaction.category_source: None},
             synchronize_session=False)
    session.query(AutoCategorizationRule).filter(
        AutoCategorizationRule.category_id == category_id,
    ).delete(synchronize_session=False)
    session.flush()
    delete_category(session, cat.id)
    return usage


def load_defaults(session: Session, user_id: int) -> tuple[int, int]:
    """Default category tree and default rules (only what is missing).
    Returns (categories created, rules created)."""
    created = seed_default_categories(session, user_id)
    rules_created = seed_default_rules(session, user_id)
    return created, rules_created


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def visible_rules(session: Session, user_id: int) -> List[AutoCategorizationRule]:
    """The user's own rules plus the family's global ones, by priority."""
    return (
        session.query(AutoCategorizationRule)
        .filter(rule_scope(session, user_id))
        .order_by(AutoCategorizationRule.priority, AutoCategorizationRule.id)
        .all()
    )


def own_rule(session: Session, user_id: int, rule_id: int) -> AutoCategorizationRule:
    """A rule this user may change: their own, never a global one."""
    rule = session.get(AutoCategorizationRule, rule_id)
    if rule is None or (rule.user_id is not None and rule.user_id != user_id):
        raise NotFound("Regla no encontrada.")
    if rule.user_id is None:
        raise AdminError("Las reglas globales solo se modifican desde la aplicación del PC.")
    return rule


def _clean_rule(session: Session, user_id: int, category_id: Optional[int], pattern: Optional[str],
                match_type: Optional[str], priority: Optional[int]) -> tuple:
    if category_id is None:
        raise AdminError("Elige la categoría que asigna la regla.")
    cat = own_category(session, user_id, category_id)
    if cat.level == CategoryLevel.type:
        raise AdminError("Elige una categoría, no un tipo.")
    pattern = (pattern or "").strip()
    if not pattern:
        raise AdminError("El texto a buscar no puede estar vacío.")
    if len(pattern) > MAX_PATTERN:
        raise AdminError(f"El texto a buscar no puede superar {MAX_PATTERN} caracteres.")
    try:
        kind = MatchType(match_type or MatchType.contains.value)
    except ValueError:
        raise AdminError("Tipo de coincidencia no válido.")
    if kind == MatchType.regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise AdminError(f"Expresión regular no válida: {exc}")
    if priority is None:
        priority = 100
    if not 1 <= priority <= 9999:
        raise AdminError("La prioridad debe estar entre 1 y 9999.")
    return cat.id, pattern, kind, priority


def add_rule(session: Session, user_id: int, category_id: Optional[int], pattern: Optional[str],
             match_type: Optional[str] = None, priority: Optional[int] = None) -> AutoCategorizationRule:
    """New personal rule (scope: only this user)."""
    cat_id, pattern, kind, priority = _clean_rule(session, user_id, category_id, pattern, match_type, priority)
    return create_rule(session, cat_id, pattern, kind, priority, user_id=user_id)


def transaction_has_rule(session: Session, user_id: int, transaction_id: int) -> bool:
    """True when an existing rule (personal or global) already matches this
    transaction's text — then the category picker offers no new rule."""
    result = apply_rules(session, [transaction_id], user_id)
    return transaction_id in result.assigned or transaction_id in result.conflicts


@dataclass
class RuleFromTransaction:
    rule: AutoCategorizationRule
    categorized_ids: List[int]      # uncategorized transactions the new rule filled at once


def add_rule_from_transaction(session: Session, user_id: int, transaction_id: int,
                              category_id: int) -> Optional[RuleFromTransaction]:
    """
    "Crear regla" from the category picker: an exact rule on the merchant
    (or, without one, the full concept) pointing at category_id, applied
    right away to the user's uncategorized transactions — what the next
    Movimientos refresh would do anyway.

    Returns None (and creates nothing) when a rule already matches the
    transaction. Raises NotFound for another user's transaction or category.
    """
    txn = session.get(Transaction, transaction_id)
    if txn is None or txn.user_id != user_id:
        raise NotFound("Movimiento no encontrado.")
    pattern = (txn.issuer_name or txn.concept or "").strip()
    if not pattern or len(pattern) > MAX_PATTERN or transaction_has_rule(session, user_id, transaction_id):
        return None
    rule = add_rule(session, user_id, category_id, pattern, MatchType.exact.value)
    ids = [row.id for row in session.query(Transaction.id).filter(
        Transaction.user_id == user_id, Transaction.category_id.is_(None))]
    result = apply_rules_and_commit(session, ids, user_id) if ids else RulesEngineResult()
    return RuleFromTransaction(rule, list(result.assigned))


def edit_rule(session: Session, user_id: int, rule_id: int, category_id: Optional[int],
              pattern: Optional[str], match_type: Optional[str],
              priority: Optional[int]) -> AutoCategorizationRule:
    rule = own_rule(session, user_id, rule_id)
    cat_id, pattern, kind, priority = _clean_rule(session, user_id, category_id, pattern, match_type, priority)
    return update_rule(session, rule.id, category_id=cat_id, match_pattern=pattern,
                       match_type=kind, priority=priority)


def remove_rule(session: Session, user_id: int, rule_id: int) -> None:
    rule = own_rule(session, user_id, rule_id)
    delete_rule(session, rule.id)


def move_rule(session: Session, user_id: int, rule_id: int, direction: str) -> AutoCategorizationRule:
    """Raise (up) or lower (down) a rule's priority past its visible
    neighbour: lower number = applied first."""
    rule = own_rule(session, user_id, rule_id)
    rules = visible_rules(session, user_id)
    index = next(i for i, r in enumerate(rules) if r.id == rule.id)
    if direction == "up":
        if index == 0:
            return rule
        neighbour = rules[index - 1]
        new_priority = max(1, neighbour.priority - 1)
    elif direction == "down":
        if index == len(rules) - 1:
            return rule
        neighbour = rules[index + 1]
        new_priority = min(9999, neighbour.priority + 1)
    else:
        raise AdminError("Dirección no válida.")
    return update_rule(session, rule.id, priority=new_priority)


def rule_match_counts(session: Session, user_id: int) -> Dict[int, int]:
    """How many of the user's transactions each rule wins today (dry run)."""
    ids = [row.id for row in session.query(Transaction.id).filter(Transaction.user_id == user_id)]
    if not ids:
        return {}
    result = apply_rules(session, ids, user_id)
    return {rule_id: len(txn_ids) for rule_id, txn_ids in result.transaction_ids_by_rule.items()}


@dataclass
class PatternPreview:
    total: int = 0
    samples: List[Transaction] = field(default_factory=list)
    manual: int = 0


def preview_pattern(session: Session, user_id: int, pattern: Optional[str],
                    match_type: Optional[str], limit: int = 8) -> PatternPreview:
    """Transactions a pattern would match (issuer or concept), newest first."""
    preview = PatternPreview()
    pattern = (pattern or "").strip()
    if not pattern or len(pattern) > MAX_PATTERN:
        return preview
    try:
        kind = MatchType(match_type or MatchType.contains.value)
    except ValueError:
        return preview
    if kind == MatchType.regex:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return preview
        test = lambda text: bool(text) and regex.search(text) is not None  # noqa: E731
    elif kind == MatchType.exact:
        low = pattern.lower()
        test = lambda text: bool(text) and text.lower() == low  # noqa: E731
    else:
        low = pattern.lower()
        test = lambda text: bool(text) and low in text.lower()  # noqa: E731

    query = (
        session.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(*TRANSACTIONS_NEWEST_FIRST)
    )
    if kind == MatchType.contains:
        like = f"%{pattern.lower()}%"
        query = query.filter(func.lower(Transaction.issuer_name).like(like)
                             | func.lower(Transaction.concept).like(like))
    for txn in query:
        if test(txn.issuer_name) or test(txn.concept):
            preview.total += 1
            if txn.category_source == CATEGORY_SOURCE_MANUAL:
                preview.manual += 1
            if len(preview.samples) < limit:
                preview.samples.append(txn)
    return preview


@dataclass
class ApplySummary:
    assigned: int
    conflicts: int
    unmatched: int


def apply_all_rules(session: Session, user_id: int, overwrite: bool = False) -> ApplySummary:
    """Run the rules over the user's transactions.

    overwrite=False: only uncategorized ones (safe default).
    overwrite=True : also re-categorize ones set by rules earlier (after
                     editing rules). Categories set by hand are never
                     touched in either mode (apply_rules_and_commit).
    """
    query = session.query(Transaction.id).filter(Transaction.user_id == user_id)
    if not overwrite:
        query = query.filter(Transaction.category_id.is_(None))
    ids = [row.id for row in query]
    if not ids:
        return ApplySummary(0, 0, 0)
    result: RulesEngineResult = apply_rules_and_commit(session, ids, user_id, overwrite=overwrite)
    return ApplySummary(len(result.assigned), len(result.conflicts), len(result.unmatched))
