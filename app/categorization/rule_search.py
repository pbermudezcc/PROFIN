"""
app/categorization/rule_search.py
=================================
FAMILYFIN — Search and filters for the Reglas screen (desktop RulesView and
web /mas/reglas). Qt-free.

``build_rule_rows`` turns the rules a user sees (``rule_scope``) into plain
rows with the category resolved the way the engine resolves it for that
user (global rules created by another family member map to the same-named
category of this user's tree, see ``rules_engine._resolve_rule_categories``).
``filter_rule_rows`` then applies the free-text search and the filters in
Python: a family has at most a few hundred rules, so this is cheap.

Free-text search: same behaviour as Movimientos — every word must appear in
at least one field (AND across words, OR across fields), case- and
accent-insensitive. Text fields match as substrings: pattern, match type
(desktop and web labels and value), category, its parent type, scope
(global / personal) and creation date (dd/mm/yyyy or ISO; only words containing "/" or "-",
so plain numbers don't hit every year). Numbers match whole: a word equal
to the priority or to the number of linked transactions.

The rule model has no active flag, no notes and no per-rule matched field
(every rule is tested against both issuer and concept), so none of those
can be searched or filtered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.analytics.transaction_search import fold_text, search_terms
from app.categorization.rules_engine import _resolve_rule_categories, get_all_rules
from app.db.models import AutoCategorizationRule, Category, MatchType

MATCH_TYPE_LABELS = {
    MatchType.contains: "Contiene",
    MatchType.exact:    "Exacto",
    MatchType.regex:    "Regex",
}
# Every wording either UI shows for a match type (the web says "Es
# exactamente" / "Expresión regular"), so both screens search the same.
MATCH_TYPE_SEARCH_WORDS = {
    MatchType.contains: "Contiene",
    MatchType.exact:    "Exacto Es exactamente",
    MatchType.regex:    "Regex Expresión regular",
}
SCOPE_GLOBAL = "global"
SCOPE_PERSONAL = "personal"
SCOPE_LABELS = {SCOPE_GLOBAL: "Global", SCOPE_PERSONAL: "Personal"}
USAGE_WITH = "con"        # rules that win at least one transaction today
USAGE_WITHOUT = "sin"     # rules that win none (candidates to review)
USAGE_LABELS = {USAGE_WITH: "Con movimientos", USAGE_WITHOUT: "Sin movimientos"}


@dataclass
class RuleRow:
    id:            int
    user_id:       Optional[int]
    pattern:       str
    match_type:    MatchType
    priority:      int
    category_id:   Optional[int]      # in this user's tree; None = no equivalent
    category_name: Optional[str]
    type_name:     Optional[str]      # parent type node (GASTO, INGRESO, ...)
    created_at:    Optional[datetime]
    matches:       Optional[int] = None    # transactions it wins today
    total:         Optional[float] = None  # their summed amount
    _haystack:     str = field(default="", repr=False)
    _numbers:      Tuple[str, ...] = field(default=(), repr=False)
    _dates:        str = field(default="", repr=False)

    @property
    def is_global(self) -> bool:
        return self.user_id is None

    @property
    def scope(self) -> str:
        return SCOPE_GLOBAL if self.is_global else SCOPE_PERSONAL

    @property
    def match_label(self) -> str:
        return MATCH_TYPE_LABELS.get(self.match_type, str(self.match_type))


@dataclass
class RuleFilters:
    text:        str = ""
    category_id: Optional[int] = None
    type_name:   Optional[str] = None
    match_type:  Optional[MatchType] = None
    scope:       Optional[str] = None     # SCOPE_GLOBAL | SCOPE_PERSONAL
    usage:       Optional[str] = None     # USAGE_WITH | USAGE_WITHOUT

    def is_empty(self) -> bool:
        return not search_terms(self.text) and all(
            value is None for value in (
                self.category_id, self.type_name, self.match_type, self.scope, self.usage)
        )


def _index(row: RuleRow) -> RuleRow:
    created = []
    if row.created_at is not None:
        created = [row.created_at.date().isoformat(), row.created_at.strftime("%d/%m/%Y")]
    texts = [
        row.pattern, MATCH_TYPE_SEARCH_WORDS.get(row.match_type, ""), row.match_type.value,
        row.category_name or "", row.type_name or "",
        SCOPE_LABELS[row.scope],
    ]
    row._haystack = "\n".join(fold_text(t) for t in texts if t)
    numbers = [str(row.priority)]
    if row.matches is not None:
        numbers.append(str(row.matches))
    row._numbers = tuple(numbers)
    row._dates = "\n".join(created)
    return row


def build_rule_rows(
    session: Session,
    user_id: int,
    matches: Optional[Dict[int, int]] = None,
    totals: Optional[Dict[int, float]] = None,
    rules: Optional[List[AutoCategorizationRule]] = None,
) -> List[RuleRow]:
    """Rows (priority order) for every rule ``user_id`` sees. ``matches``
    and ``totals`` (rule id -> count / amount) are optional usage stats;
    when ``matches`` is given, rules missing from it count 0."""
    if rules is None:
        rules = get_all_rules(session, user_id=user_id)
    if not rules:
        return []
    resolved = _resolve_rule_categories(session, rules, user_id)
    wanted = set(resolved.values()) | {rule.category_id for rule in rules}
    cats: Dict[int, Category] = {
        c.id: c for c in session.query(Category).filter(Category.id.in_(wanted))
    }
    rows: List[RuleRow] = []
    for rule in rules:
        own_id = resolved.get(rule.id)
        # Own tree first; for a global rule without equivalent, show its
        # creator's category name so the row still reads sensibly.
        cat = cats.get(own_id) if own_id is not None else cats.get(rule.category_id)
        parent = cat.parent if cat is not None else None
        rows.append(_index(RuleRow(
            id=rule.id,
            user_id=rule.user_id,
            pattern=rule.match_pattern,
            match_type=MatchType(rule.match_type),
            priority=rule.priority,
            category_id=own_id,
            category_name=cat.name if cat is not None else None,
            type_name=parent.name if parent is not None else None,
            created_at=rule.created_at,
            matches=(matches.get(rule.id, 0) if matches is not None else None),
            total=(round(totals.get(rule.id, 0.0), 2) if totals is not None else None),
        )))
    return rows


def _matches_text(row: RuleRow, terms: Iterable[str]) -> bool:
    return all(
        term in row._haystack
        or term in row._numbers
        # Dates only for words that look like one ("18/10", "2026-10"), so a
        # plain number such as a priority doesn't match every creation year.
        or (("/" in term or "-" in term) and term in row._dates)
        for term in terms
    )


def filter_rule_rows(rows: List[RuleRow], filters: RuleFilters) -> List[RuleRow]:
    """Rows passing every filter and containing every search word."""
    terms = search_terms(filters.text)
    out: List[RuleRow] = []
    for row in rows:
        if filters.category_id is not None and row.category_id != filters.category_id:
            continue
        if filters.type_name is not None and fold_text(row.type_name or "") != fold_text(filters.type_name):
            continue
        if filters.match_type is not None and row.match_type != filters.match_type:
            continue
        if filters.scope is not None and row.scope != filters.scope:
            continue
        if filters.usage is not None and row.matches is not None:
            if (row.matches > 0) != (filters.usage == USAGE_WITH):
                continue
        if terms and not _matches_text(row, terms):
            continue
        out.append(row)
    return out


def filter_options(rows: List[RuleRow]) -> Tuple[List[Tuple[int, str, str]], List[str]]:
    """(categories as (id, name, type name) sorted by type then name,
    type names sorted) present among ``rows`` — what the combos offer."""
    cats = {
        row.category_id: (row.category_id, row.category_name or "", row.type_name or "")
        for row in rows if row.category_id is not None
    }
    categories = sorted(cats.values(), key=lambda c: (fold_text(c[2]), fold_text(c[1])))
    types = sorted({row.type_name for row in rows if row.type_name}, key=fold_text)
    return categories, types


def count_label(shown: int, total: int, filtered: bool) -> str:
    """"Mostrando 37 de 412 reglas" when something filters, else "412 reglas"."""
    noun = "regla" if total == 1 else "reglas"
    if filtered:
        return f"Mostrando {shown} de {total} {noun}"
    return f"{total} {noun}"
