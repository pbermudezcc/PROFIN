"""
app/categorization/rules_engine.py
====================================
FAMILYFIN — Auto-Categorization Rules Engine

Responsibility:
  Apply AutoCategorizationRule entries to a set of transactions,
  assigning category_id automatically based on pattern matching.

Matching logic:
  - Fields checked (OR gate — match in EITHER field triggers the rule):
      * issuer_name
      * concept
  - Movement type: a rule with transaction_type set (expense / income)
    only matches movements of that type (Transaction.type, from the sign
    of the amount); NULL matches any. The same text («BIZUM», a shop that
    also refunds) can be a payment or a receipt with different categories.
  - Match types:
      contains : case-insensitive substring match
      exact    : case-insensitive full-string match
      regex    : Python re.search() match (case-insensitive)
  - Priority: lower number = higher priority (rule with priority=1
    beats priority=100). First match wins.
  - Conflict detection: if two rules of different categories match the
    SAME transaction at DIFFERENT priorities, the higher-priority rule
    wins silently. If two rules match at the SAME priority, a conflict
    is flagged and the user is notified (no auto-assignment).

Global vs. per-user rules:
  - AutoCategorizationRule.user_id = NULL  → global rule (all users)
  - AutoCategorizationRule.user_id = X     → rule for user X only
  - Per-user rules override global rules at the SAME priority.
  - Load order: global rules first, then user rules (user rules can
    shadow global rules at equal priority).
  - Categories belong to one user, so a global rule's category_id points
    into its creator's tree. For any other user the rule is applied to the
    equivalent category in that user's own tree (same name and same parent
    type name); if there is none, the global rule is skipped for that user.
  - The demo account (config.DEMO_USERNAME) never sees or applies global
    rules: see rule_scope().

Output:
  RulesEngineResult.assigned   : {transaction_id: category_id}
  RulesEngineResult.conflicts  : {transaction_id: [RuleConflict, ...]}
  RulesEngineResult.unmatched  : [transaction_id, ...]
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import DEMO_USERNAME
from app.db.models import (
    CATEGORY_SOURCE_MANUAL,
    CATEGORY_SOURCE_RULE,
    AutoCategorizationRule,
    Category,
    MatchType,
    Transaction,
    TransactionType,
    User,
)

logger = logging.getLogger(__name__)

# The movement types a rule can be limited to. Transaction.type comes from
# the sign of the amount, so it is never "investment": an investment
# contribution is an expense movement filed under an INVERSION category.
RULE_TRANSACTION_TYPES = (TransactionType.expense, TransactionType.income)

# update_rule(): "leave transaction_type as it is" (None means "any type").
_UNCHANGED = object()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RuleConflict:
    """Two rules matched the same transaction at the same priority."""
    transaction_id: int
    rule_a_id:      int
    rule_b_id:      int
    category_a_id:  int
    category_b_id:  int
    priority:       int
    field_matched:  str   # 'issuer_name' | 'concept' | 'both'
    pattern_a:      str
    pattern_b:      str


@dataclass
class RulesEngineResult:
    """
    Result of running the rules engine over a batch of transactions.

    Attributes
    ----------
    assigned  : dict {transaction_id: category_id} — auto-assigned.
    assigned_by_rule : dict {rule_id: transaction_count} — assignments won
                       by each rule.
    transaction_ids_by_rule : dict {rule_id: [transaction_id, ...]} —
                              transactions assigned by each rule.
    conflicts : dict {transaction_id: List[RuleConflict]} — needs user input.
    unmatched : List[transaction_id] — no rule matched.
    applied   : int — total rules applied (assigned + conflict rows).
    transactions_by_id : dict {transaction_id: Transaction} — the ORM
                          objects already loaded while matching, so callers
                          (e.g. apply_rules_and_commit) don't need to
                          re-fetch them one by one.
    """
    assigned:  Dict[int, int]               = field(default_factory=dict)
    assigned_by_rule: Dict[int, int]        = field(default_factory=dict)
    transaction_ids_by_rule: Dict[int, List[int]] = field(
        default_factory=dict
    )
    conflicts: Dict[int, List[RuleConflict]] = field(default_factory=dict)
    unmatched: List[int]                    = field(default_factory=list)
    transactions_by_id: Dict[int, Transaction] = field(default_factory=dict)

    @property
    def applied(self) -> int:
        return len(self.assigned) + len(self.conflicts)

    @property
    def summary(self) -> str:
        return (
            f"Asignadas: {len(self.assigned)} | "
            f"Conflictos: {len(self.conflicts)} | "
            f"Sin categoría: {len(self.unmatched)}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_rules(
    session:        Session,
    transaction_ids: List[int],
    user_id:        int,
) -> RulesEngineResult:
    """
    Apply all applicable rules to the given transaction IDs.

    Rules are loaded ordered by priority ASC (lowest = highest priority).
    Global rules (user_id IS NULL) are loaded first, then user-specific
    rules. At equal priority, user rules shadow global rules.

    Parameters
    ----------
    session         : Active SQLAlchemy session.
    transaction_ids : List of transaction primary keys to process.
    user_id         : Authenticated user (for loading per-user rules).

    Returns
    -------
    RulesEngineResult
    """
    result = RulesEngineResult()

    if not transaction_ids:
        return result

    # -- Load rules (global + user-specific), ordered by priority ASC ------
    rules = _load_rules(session, user_id)
    category_by_rule_id = _resolve_rule_categories(session, rules, user_id)
    rules = [rule for rule in rules if rule.id in category_by_rule_id]
    if not rules:
        logger.info("No hay reglas de auto-categorización definidas.")
        result.unmatched = list(transaction_ids)
        return result

    # -- Load transactions ---------------------------------------------------
    transactions = (
        session.query(Transaction)
        .filter(Transaction.id.in_(transaction_ids))
        .all()
    )

    # -- Precompute each rule's lower-cased pattern once, instead of on
    #    every rule x field x transaction comparison. --------------------
    pattern_lower_by_rule_id = {
        rule.id: rule.match_pattern.lower() for rule in rules
    }

    # -- Apply rules to each transaction ------------------------------------
    for txn in transactions:
        _process_transaction(
            txn, rules, result, pattern_lower_by_rule_id, category_by_rule_id
        )

    logger.info(
        "Rules engine completado: %s", result.summary
    )
    result.transactions_by_id = {txn.id: txn for txn in transactions}
    return result


def apply_rules_and_commit(
    session:         Session,
    transaction_ids: List[int],
    user_id:         int,
    overwrite:       bool = False,
) -> RulesEngineResult:
    """
    Apply rules AND persist the assignments to the DB.

    Parameters
    ----------
    overwrite : If True, re-categorize transactions that already have a
                category_id (used when rules are edited/deleted).
                If False (default), only categorize transactions with
                category_id=NULL (used on first import).
                Categories a user set by hand (category_source='manual')
                are never overwritten, in either mode.

    Conflicted and unmatched transactions are left untouched.
    """
    result = apply_rules(session, transaction_ids, user_id)

    # Write assignments, reusing the Transaction objects apply_rules already
    # loaded instead of re-fetching each one individually.
    for txn_id, cat_id in result.assigned.items():
        txn = result.transactions_by_id.get(txn_id)
        if txn is None or txn.category_source == CATEGORY_SOURCE_MANUAL:
            continue
        if overwrite or txn.category_id is None:
            txn.category_id = cat_id
            txn.category_source = CATEGORY_SOURCE_RULE

    session.commit()
    logger.info(
        "Auto-categorización guardada: %d asignaciones (overwrite=%s).",
        len(result.assigned), overwrite,
    )
    return result


def create_rule(
    session:       Session,
    category_id:   int,
    match_pattern: str,
    match_type:    MatchType  = MatchType.contains,
    priority:      int        = 100,
    user_id:       Optional[int] = None,
    transaction_type: Optional[TransactionType] = None,
) -> AutoCategorizationRule:
    """
    Create and persist a new auto-categorization rule.

    Parameters
    ----------
    session       : Active SQLAlchemy session.
    category_id   : Category to assign when this rule matches.
    match_pattern : Pattern string (substring, exact string, or regex).
    match_type    : contains | exact | regex.
    priority      : Lower = higher priority. Default 100.
    user_id       : None = global rule. User ID = per-user rule.
    transaction_type : Only movements of this type match (expense /
                       income). None = any type.

    Returns
    -------
    AutoCategorizationRule (persisted).

    Raises
    ------
    ValueError : If match_pattern is empty, regex is invalid or the
                 movement type is not one of RULE_TRANSACTION_TYPES.
    """
    if not match_pattern or not match_pattern.strip():
        raise ValueError("El patrón de la regla no puede estar vacío.")
    transaction_type = _check_transaction_type(transaction_type)

    if match_type == MatchType.regex:
        try:
            re.compile(match_pattern)
        except re.error as exc:
            raise ValueError(
                f"Expresión regular inválida '{match_pattern}': {exc}"
            ) from exc

    rule = AutoCategorizationRule(
        user_id       = user_id,
        category_id   = category_id,
        match_pattern = match_pattern.strip(),
        match_type    = match_type,
        priority      = priority,
        transaction_type = transaction_type,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    logger.info(
        "Regla creada: id=%d pattern='%s' type=%s priority=%d user=%s movimiento=%s",
        rule.id, rule.match_pattern, rule.match_type.value,
        rule.priority, user_id,
        rule.transaction_type.value if rule.transaction_type else "cualquiera",
    )
    return rule


def delete_rule(session: Session, rule_id: int) -> None:
    """
    Delete an auto-categorization rule by ID.

    Raises
    ------
    ValueError : If rule not found.
    """
    rule = session.get(AutoCategorizationRule, rule_id)
    if rule is None:
        raise ValueError(f"Regla con id={rule_id} no encontrada.")
    session.delete(rule)
    session.commit()
    logger.info("Regla id=%d eliminada.", rule_id)


def update_rule(
    session:       Session,
    rule_id:       int,
    category_id:   Optional[int]  = None,
    match_pattern: Optional[str]  = None,
    match_type:    Optional[MatchType] = None,
    priority:      Optional[int]  = None,
    transaction_type = _UNCHANGED,
) -> AutoCategorizationRule:
    """
    Update one or more fields of an existing auto-categorization rule.

    Only non-None arguments are applied — omitted arguments keep their
    current value. transaction_type is the exception, because None means
    "any type": it is only changed when passed.

    Raises
    ------
    ValueError : If rule not found, pattern is empty, or regex is invalid.
    """
    rule = session.get(AutoCategorizationRule, rule_id)
    if rule is None:
        raise ValueError(f"Regla con id={rule_id} no encontrada.")

    if match_pattern is not None:
        if not match_pattern.strip():
            raise ValueError("El patrón de la regla no puede estar vacío.")
        rule.match_pattern = match_pattern.strip()

    if match_type is not None:
        # Validate regex before saving
        if match_type == MatchType.regex:
            effective_pattern = match_pattern.strip() if match_pattern else rule.match_pattern
            try:
                re.compile(effective_pattern)
            except re.error as exc:
                raise ValueError(
                    f"Expresión regular inválida '{effective_pattern}': {exc}"
                ) from exc
        rule.match_type = match_type

    if category_id is not None:
        rule.category_id = category_id

    if priority is not None:
        rule.priority = priority

    if transaction_type is not _UNCHANGED:
        rule.transaction_type = _check_transaction_type(transaction_type)

    session.commit()
    session.refresh(rule)
    logger.info(
        "Regla actualizada: id=%d pattern='%s' type=%s priority=%d",
        rule.id, rule.match_pattern, rule.match_type.value, rule.priority,
    )
    return rule


def shares_global_rules(session: Session, user_id: int) -> bool:
    """False for the demo account, which is shown to visitors."""
    return session.query(User.id).filter(
        User.id == user_id, func.upper(User.username) == DEMO_USERNAME.upper()
    ).first() is None


def rule_scope(session: Session, user_id: int):
    """Filter for the rules a user sees and gets applied: their own plus the
    family's global ones (only their own for the demo account)."""
    own = AutoCategorizationRule.user_id == user_id
    if not shares_global_rules(session, user_id):
        return own
    return own | AutoCategorizationRule.user_id.is_(None)


def get_all_rules(
    session: Session,
    user_id: Optional[int] = None,
) -> List[AutoCategorizationRule]:
    """
    Return rules ordered by priority ASC.
    If user_id is provided, returns the rules in rule_scope() for that user.
    If user_id is None, returns only global rules.
    """
    query = session.query(AutoCategorizationRule)
    if user_id is not None:
        query = query.filter(rule_scope(session, user_id))
    else:
        query = query.filter(AutoCategorizationRule.user_id == None)  # noqa: E711
    return query.order_by(
        AutoCategorizationRule.priority.asc(),
        AutoCategorizationRule.id.asc(),
    ).all()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_transaction_type(value) -> Optional[TransactionType]:
    """None or one of RULE_TRANSACTION_TYPES (enum or its value)."""
    if value is None:
        return None
    try:
        value = TransactionType(value)
    except ValueError:
        value = None
    if value not in RULE_TRANSACTION_TYPES:
        raise ValueError("Tipo de movimiento de la regla no válido (gasto, ingreso o cualquiera).")
    return value


def _load_rules(
    session: Session,
    user_id: int,
) -> List[AutoCategorizationRule]:
    """Load global + user-specific rules, ordered by priority ASC."""
    return get_all_rules(session, user_id=user_id)


def _resolve_rule_categories(
    session: Session,
    rules:   List[AutoCategorizationRule],
    user_id: int,
) -> Dict[int, int]:
    """Map each applicable rule id to a category id in user_id's own tree.

    Rules whose category already belongs to the user map to it unchanged.
    Global rules pointing at another user's category map to the category
    with the same name under a type node with the same name; rules with no
    equivalent are left out (never assign another user's category).
    """
    category_ids = {rule.category_id for rule in rules}
    categories = {
        cat.id: cat
        for cat in session.query(Category).filter(Category.id.in_(category_ids))
    } if category_ids else {}

    def key(cat: Category) -> tuple:
        parent_name = cat.parent.name if cat.parent is not None else None
        return (parent_name, cat.name, cat.level)

    foreign = [
        cat for cat in categories.values() if cat.user_id != user_id
    ]
    own_by_key: Dict[tuple, int] = {}
    if foreign:
        wanted_names = {cat.name for cat in foreign}
        for cat in (
            session.query(Category)
            .filter(Category.user_id == user_id, Category.name.in_(wanted_names))
        ):
            own_by_key.setdefault(key(cat), cat.id)

    resolved: Dict[int, int] = {}
    for rule in rules:
        cat = categories.get(rule.category_id)
        if cat is None:
            continue
        if cat.user_id == user_id:
            resolved[rule.id] = cat.id
        elif rule.user_id is None and key(cat) in own_by_key:
            resolved[rule.id] = own_by_key[key(cat)]
        else:
            logger.debug(
                "Regla id=%d omitida para user_id=%d: sin categoría equivalente.",
                rule.id, user_id,
            )
    return resolved


def _match(
    rule:        AutoCategorizationRule,
    value:       Optional[str],
    value_lower: Optional[str],
    pattern_lower: str,
) -> bool:
    """
    Test a single (pre-lowercased) field value against a rule's pattern.
    Returns True if the pattern matches, False otherwise.

    `value_lower`/`pattern_lower` are precomputed by the caller (once per
    transaction / once per rule respectively) instead of being lower-cased
    again on every rule x field combination.
    """
    if not value_lower:
        return False

    if rule.match_type == MatchType.contains:
        return pattern_lower in value_lower

    elif rule.match_type == MatchType.exact:
        return value_lower == pattern_lower

    elif rule.match_type == MatchType.regex:
        try:
            return bool(re.search(rule.match_pattern, value, re.IGNORECASE))
        except re.error:
            logger.warning(
                "Regex inválida en regla id=%d: '%s'", rule.id, rule.match_pattern
            )
            return False

    return False


def _match_transaction(
    rule:          AutoCategorizationRule,
    txn:           Transaction,
    issuer_lower:  Optional[str],
    concept_lower: Optional[str],
    pattern_lower: str,
) -> Optional[str]:
    """
    Test a rule against a transaction using OR gate on issuer_name + concept.

    Returns
    -------
    str  : 'issuer_name' | 'concept' | 'both' if matched.
    None : if no match.
    """
    hit_issuer  = _match(rule, txn.issuer_name, issuer_lower, pattern_lower)
    hit_concept = _match(rule, txn.concept, concept_lower, pattern_lower)

    if hit_issuer and hit_concept:
        return "both"
    elif hit_issuer:
        return "issuer_name"
    elif hit_concept:
        return "concept"
    return None


def _process_transaction(
    txn:            Transaction,
    rules:          List[AutoCategorizationRule],
    result:         RulesEngineResult,
    pattern_lower_by_rule_id: Dict[int, str],
    category_by_rule_id: Dict[int, int],
) -> None:
    """
    Apply all rules to a single transaction and update result in-place.

    Logic:
    - Iterate rules in priority order (already sorted ASC).
    - First match at priority P assigns the transaction.
    - If a SECOND rule at the SAME priority P also matches and maps to a
      DIFFERENT category → conflict flagged, no auto-assignment.
    - Once a priority level produces a match (assign or conflict),
      lower-priority rules are ignored.
    """
    best_priority:  Optional[int]  = None
    best_rule:      Optional[AutoCategorizationRule] = None
    best_field:     Optional[str]  = None
    conflict_rules: List[tuple]    = []  # (rule, field_matched)

    issuer_lower  = txn.issuer_name.lower() if txn.issuer_name else None
    concept_lower = txn.concept.lower() if txn.concept else None

    for rule in rules:
        if rule.transaction_type is not None and rule.transaction_type != txn.type:
            continue
        field_hit = _match_transaction(
            rule, txn, issuer_lower, concept_lower,
            pattern_lower_by_rule_id[rule.id],
        )
        if field_hit is None:
            continue

        if best_priority is None:
            # First match
            best_priority = rule.priority
            best_rule     = rule
            best_field    = field_hit
            conflict_rules = [(rule, field_hit)]

        elif rule.priority == best_priority:
            # Same priority level — potential conflict
            if category_by_rule_id[rule.id] != category_by_rule_id[best_rule.id]:
                conflict_rules.append((rule, field_hit))

        else:
            # Lower priority (higher number) — stop searching
            break

    if not conflict_rules:
        # No match at all
        result.unmatched.append(txn.id)

    elif len(conflict_rules) == 1:
        # Single winner — assign
        result.assigned[txn.id] = category_by_rule_id[best_rule.id]
        result.assigned_by_rule[best_rule.id] = (
            result.assigned_by_rule.get(best_rule.id, 0) + 1
        )
        result.transaction_ids_by_rule.setdefault(best_rule.id, []).append(
            txn.id
        )

    else:
        # Multiple rules at same priority, different categories → conflict
        conflicts = []
        first_rule, first_field = conflict_rules[0]
        for other_rule, other_field in conflict_rules[1:]:
            conflicts.append(RuleConflict(
                transaction_id = txn.id,
                rule_a_id      = first_rule.id,
                rule_b_id      = other_rule.id,
                category_a_id  = category_by_rule_id[first_rule.id],
                category_b_id  = category_by_rule_id[other_rule.id],
                priority       = best_priority,
                field_matched  = other_field,
                pattern_a      = first_rule.match_pattern,
                pattern_b      = other_rule.match_pattern,
            ))
        result.conflicts[txn.id] = conflicts
        logger.warning(
            "Conflicto de reglas para transacción id=%d: "
            "%d reglas coinciden en prioridad=%d",
            txn.id, len(conflict_rules), best_priority,
        )