"""
app/categorization/library_io.py
================================
FAMILYFIN — Export / import of a user's categorization library.

The library is the category tree (type → category) plus the
auto-categorization rules: the work that makes imports categorize
themselves. It is written as a small, versioned, deterministic JSON file
(".profin-cats.json") meant to be archived, diffed and loaded into another
user or database.

Design rules
------------
* Never trust ids: categories are identified by their natural key
  (type name, category name), compared case-insensitively — the same names
  repeat under different types (lessons_learned: ids from other users).
* Import is planned first (plan_import, read-only) and then applied in one
  transaction (apply_import). The plan is recomputed at apply time so it
  always reflects the current state of the database.
* Merge (default) only adds what is missing. Replace also makes the user's
  *personal rules* equal to the file. Categories are never deleted and
  transactions are never touched — rules are applied afterwards, on demand.
* Global rules (user_id NULL) are only imported when allow_global=True
  (desktop); the web never changes them.
* A category's Fijo / Traspaso mark travels with it ("natures", optional)
  and is only set where the user has none yet. The «Con» member of a
  traspaso is person-specific and never travels.

Qt-free; shared by CategoriesView and the web router.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.categorization.admin_service import MAX_NAME, MAX_PATTERN, AdminError
from app.categorization.rules_engine import RULE_TRANSACTION_TYPES, rule_scope
from app.db.models import (
    CATEGORY_NATURES,
    AutoCategorizationRule,
    Category,
    CategoryLevel,
    MatchType,
    TransactionType,
)

FORMAT_NAME = "profin.categorization"
FORMAT_VERSION = 1
FILE_SUFFIX = ".profin-cats.json"
MAX_FILE_BYTES = 2 * 1024 * 1024        # compressed or not
MAX_RULES = 5000
MAX_CATEGORIES = 2000

SCOPE_PERSONAL = "personal"
SCOPE_GLOBAL = "global"
MODE_MERGE = "merge"
MODE_REPLACE = "replace"

# Upgrade steps for older files: {from_version: fn(dict) -> dict at from_version + 1}.
# Same idea as SCHEMA_MIGRATIONS: never renumber, only append.
_UPGRADES: Dict[int, Callable[[dict], dict]] = {}


class LibraryError(AdminError):
    """The file cannot be imported; the message is shown to the user."""


# ---------------------------------------------------------------------------
# File model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LibType:
    name: str
    transaction_type: Optional[str]
    categories: Tuple[str, ...]
    # ((category name, CATEGORY_NATURE_*), ...) for the non-variable ones;
    # optional in the file (older files: everything variable).
    natures: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LibRule:
    type: str
    category: str
    pattern: str
    match_type: str
    priority: int
    scope: str
    # Movement type the rule is limited to ("expense" / "income"); None =
    # any. Optional in the file: files written before it existed mean any.
    transaction_type: Optional[str] = None


@dataclass
class LibraryFile:
    types: List[LibType]
    rules: List[LibRule]
    source_user: Optional[str] = None
    exported_at: Optional[str] = None
    app_version: Optional[str] = None
    checksum_ok: bool = True        # False: edited or damaged by hand → warn

    @property
    def category_count(self) -> int:
        return sum(len(t.categories) for t in self.types)


def _fold(text: str) -> str:
    return text.strip().casefold()


def _rule_key(type_name: str, category: str, pattern: str, match_type: str,
              transaction_type: Optional[str] = None) -> tuple:
    return (_fold(type_name), _fold(category), _fold(pattern), match_type, transaction_type)


def _txn_type_value(rule: AutoCategorizationRule) -> Optional[str]:
    return rule.transaction_type.value if rule.transaction_type else None


def movement_note(transaction_type: Optional[str]) -> str:
    """" (solo gastos)" / " (solo ingresos)" for previews; "" for any type."""
    return {TransactionType.expense.value: " (solo gastos)",
            TransactionType.income.value: " (solo ingresos)"}.get(transaction_type or "", "")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _type_and_category(cat: Category) -> Optional[Tuple[Category, Category]]:
    """(type node, category) for an assignable category, else None."""
    parent = cat.parent
    if cat.level == CategoryLevel.category and parent is not None and parent.level == CategoryLevel.type:
        return parent, cat
    return None


def export_library(session: Session, user_id: int, include_global: bool = False,
                   source_user: Optional[str] = None) -> dict:
    """The user's library as a plain dict (see dumps() for the file bytes)."""
    from app.config import APP_VERSION
    from app.db.database import SCHEMA_VERSION

    roots = (
        session.query(Category)
        .filter(Category.user_id == user_id, Category.parent_id.is_(None),
                Category.level == CategoryLevel.type)
        .all()
    )
    types = []
    for root in sorted(roots, key=lambda c: (_fold(c.name), c.name)):
        children = sorted(
            (c.name for c in root.children if c.level == CategoryLevel.category),
            key=lambda n: (_fold(n), n),
        )
        entry = {
            "name": root.name,
            "transaction_type": root.transaction_type.value if root.transaction_type else None,
            "categories": children,
        }
        natures = {c.name: c.nature for c in root.children
                   if c.level == CategoryLevel.category and c.nature in CATEGORY_NATURES}
        if natures:
            entry["natures"] = natures
        types.append(entry)

    query = session.query(AutoCategorizationRule)
    if include_global:
        query = query.filter(rule_scope(session, user_id))
    else:
        query = query.filter(AutoCategorizationRule.user_id == user_id)

    rules, seen = [], set()
    for rule in query:
        pair = _type_and_category(rule.category)
        if pair is None:
            continue
        type_node, cat = pair
        scope = SCOPE_GLOBAL if rule.user_id is None else SCOPE_PERSONAL
        key = _rule_key(type_node.name, cat.name, rule.match_pattern, rule.match_type.value,
                        _txn_type_value(rule)) + (scope,)
        if key in seen:
            continue
        seen.add(key)
        rules.append({
            "type": type_node.name,
            "category": cat.name,
            "pattern": rule.match_pattern,
            "match_type": rule.match_type.value,
            "transaction_type": _txn_type_value(rule),
            "priority": rule.priority,
            "scope": scope,
        })
    rules.sort(key=lambda r: (r["priority"], _fold(r["type"]), _fold(r["category"]),
                              _fold(r["pattern"]), r["match_type"], r["transaction_type"] or "",
                              r["scope"]))

    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": APP_VERSION,
        "db_schema_version": SCHEMA_VERSION,
        "source_user": source_user,
        "types": types,
        "rules": rules,
    }


def _canonical(payload: dict) -> bytes:
    body = {k: v for k, v in payload.items() if k != "checksum"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def dumps(payload: dict) -> bytes:
    """File bytes: pretty, key-sorted JSON with a SHA-256 of the content."""
    body = {k: v for k, v in payload.items() if k != "checksum"}
    body["checksum"] = "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()
    return (json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def suggested_filename(user_name: str, when: Optional[datetime] = None) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", user_name or "usuario").strip("_") or "usuario"
    return f"profin-categorias-{safe}-{(when or datetime.now()):%Y%m%d}{FILE_SUFFIX}"


# ---------------------------------------------------------------------------
# Parse + validate
# ---------------------------------------------------------------------------

def _decompress(raw: bytes) -> bytes:
    if raw[:2] != b"\x1f\x8b":
        return raw
    # Bounded inflate: a tiny .gz must not expand into gigabytes.
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(raw, MAX_FILE_BYTES + 1)
    except zlib.error:
        raise LibraryError("El archivo comprimido está dañado.")
    if len(data) > MAX_FILE_BYTES or inflater.unconsumed_tail:
        raise LibraryError("El archivo es demasiado grande.")
    return data


def _text(value, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LibraryError(f"Falta {what} o no es texto.")
    value = value.strip()
    if len(value) > limit:
        raise LibraryError(f"{what.capitalize()} supera {limit} caracteres: «{value[:40]}…».")
    return value


def parse_library(raw: bytes) -> LibraryFile:
    """Validate file bytes (plain or gzip JSON) into a LibraryFile.

    Raises LibraryError with a user-facing message on anything malformed.
    A checksum mismatch is not an error: checksum_ok=False lets the caller warn.
    """
    if len(raw) > MAX_FILE_BYTES:
        raise LibraryError("El archivo es demasiado grande.")
    data = _decompress(raw)
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LibraryError("El archivo no es un JSON válido.")
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_NAME:
        raise LibraryError("El archivo no es una biblioteca de categorías de FAMILYFIN.")

    checksum = payload.get("checksum")
    checksum_ok = (isinstance(checksum, str)
                   and checksum == "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest())

    version = payload.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise LibraryError("Versión de formato no válida.")
    if version > FORMAT_VERSION:
        raise LibraryError(
            f"El archivo usa la versión de formato {version}; esta aplicación solo entiende "
            f"hasta la {FORMAT_VERSION}. Actualiza FAMILYFIN para importarlo.")
    while version < FORMAT_VERSION:
        upgrade = _UPGRADES.get(version)
        if upgrade is None:
            raise LibraryError(f"No se puede actualizar el formato {version}.")
        payload = upgrade(payload)
        version += 1

    raw_types = payload.get("types")
    raw_rules = payload.get("rules")
    if not isinstance(raw_types, list) or not isinstance(raw_rules, list):
        raise LibraryError("Al archivo le faltan las secciones de tipos o reglas.")
    if len(raw_rules) > MAX_RULES:
        raise LibraryError(f"El archivo tiene más de {MAX_RULES} reglas.")

    tx_types = {t.value for t in TransactionType}
    types: List[LibType] = []
    seen_types: Dict[str, LibType] = {}
    total = 0
    for entry in raw_types:
        if not isinstance(entry, dict):
            raise LibraryError("Tipo de categoría mal formado.")
        name = _text(entry.get("name"), "el nombre de un tipo", MAX_NAME)
        tx = entry.get("transaction_type")
        if tx is not None and tx not in tx_types:
            raise LibraryError(f"Tipo de movimiento desconocido en «{name}»: {tx}.")
        cats_raw = entry.get("categories") or []
        if not isinstance(cats_raw, list):
            raise LibraryError(f"Las categorías de «{name}» están mal formadas.")
        cats, seen = [], set()
        for cat in cats_raw:
            cat = _text(cat, f"el nombre de una categoría de «{name}»", MAX_NAME)
            if _fold(cat) not in seen:
                seen.add(_fold(cat))
                cats.append(cat)
        total += len(cats)
        if _fold(name) in seen_types:
            raise LibraryError(f"El tipo «{name}» aparece dos veces.")
        natures_raw = entry.get("natures") or {}
        if not isinstance(natures_raw, dict):
            raise LibraryError(f"Los tipos de gasto de «{name}» están mal formados.")
        known = {_fold(c): c for c in cats}
        natures = []
        for cat, nature in natures_raw.items():
            if nature not in CATEGORY_NATURES or not isinstance(cat, str) or _fold(cat) not in known:
                raise LibraryError(f"Tipo de gasto no válido en «{name}»: {cat} = {nature}.")
            natures.append((known[_fold(cat)], nature))
        lib_type = LibType(name, tx, tuple(cats), tuple(sorted(natures)))
        seen_types[_fold(name)] = lib_type
        types.append(lib_type)
    if total > MAX_CATEGORIES:
        raise LibraryError(f"El archivo tiene más de {MAX_CATEGORIES} categorías.")

    match_types = {m.value for m in MatchType}
    rules: List[LibRule] = []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            raise LibraryError("Regla mal formada.")
        type_name = _text(entry.get("type"), "el tipo de una regla", MAX_NAME)
        category = _text(entry.get("category"), "la categoría de una regla", MAX_NAME)
        lib_type = seen_types.get(_fold(type_name))
        if lib_type is None or _fold(category) not in {_fold(c) for c in lib_type.categories}:
            raise LibraryError(f"La regla apunta a «{type_name} › {category}», que no está en el archivo.")
        pattern = _text(entry.get("pattern"), "el texto de una regla", MAX_PATTERN)
        kind = entry.get("match_type", MatchType.contains.value)
        if kind not in match_types:
            raise LibraryError(f"Tipo de coincidencia no válido en la regla «{pattern}».")
        if kind == MatchType.regex.value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise LibraryError(f"Expresión regular no válida «{pattern}»: {exc}")
        priority = entry.get("priority", 100)
        if not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 9999:
            raise LibraryError(f"Prioridad fuera de rango (1–9999) en la regla «{pattern}».")
        scope = entry.get("scope", SCOPE_PERSONAL)
        if scope not in (SCOPE_PERSONAL, SCOPE_GLOBAL):
            raise LibraryError(f"Ámbito no válido en la regla «{pattern}».")
        rule_tx = entry.get("transaction_type")
        if rule_tx is not None and rule_tx not in {t.value for t in RULE_TRANSACTION_TYPES}:
            raise LibraryError(f"Tipo de movimiento no válido en la regla «{pattern}».")
        rules.append(LibRule(type_name, category, pattern, kind, priority, scope, rule_tx))

    def _meta(key: str) -> Optional[str]:
        value = payload.get(key)
        return value[:100] if isinstance(value, str) else None

    return LibraryFile(types, rules, source_user=_meta("source_user"),
                       exported_at=_meta("exported_at"), app_version=_meta("app_version"),
                       checksum_ok=checksum_ok)


# ---------------------------------------------------------------------------
# Plan + apply
# ---------------------------------------------------------------------------

@dataclass
class ImportPlan:
    mode: str
    new_types: List[str] = field(default_factory=list)
    new_categories: List[str] = field(default_factory=list)          # "TIPO › Categoría"
    nature_changes: List[str] = field(default_factory=list)          # "TIPO › Categoría: fijo"
    new_rules: List[LibRule] = field(default_factory=list)
    priority_changes: List[Tuple[LibRule, int]] = field(default_factory=list)   # (rule, old priority)
    deleted_rules: List[str] = field(default_factory=list)          # replace: personal rules gone
    unchanged_rules: int = 0
    skipped_global: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.new_types or self.new_categories or self.nature_changes or self.new_rules
                    or self.priority_changes or self.deleted_rules)


@dataclass
class _State:
    """The user's current tree and rules, indexed by natural key."""
    types: Dict[str, Category]
    categories: Dict[Tuple[str, str], Category]
    personal: Dict[tuple, AutoCategorizationRule]
    global_: Dict[tuple, AutoCategorizationRule]
    stray_personal: List[AutoCategorizationRule]   # rules on non-exportable categories


def _load_state(session: Session, user_id: int) -> _State:
    types: Dict[str, Category] = {}
    categories: Dict[Tuple[str, str], Category] = {}
    for cat in session.query(Category).filter(Category.user_id == user_id):
        if cat.level == CategoryLevel.type and cat.parent_id is None:
            types.setdefault(_fold(cat.name), cat)
        pair = _type_and_category(cat)
        if pair is not None:
            categories.setdefault((_fold(pair[0].name), _fold(cat.name)), cat)

    personal, global_, stray = {}, {}, []
    rules = (session.query(AutoCategorizationRule)
             .filter(rule_scope(session, user_id))
             .order_by(AutoCategorizationRule.id))
    for rule in rules:
        pair = _type_and_category(rule.category)
        if pair is None:
            if rule.user_id is not None:
                stray.append(rule)
            continue
        key = _rule_key(pair[0].name, pair[1].name, rule.match_pattern, rule.match_type.value,
                        _txn_type_value(rule))
        target = global_ if rule.user_id is None else personal
        target.setdefault(key, rule)
    return _State(types, categories, personal, global_, stray)


def _describe(rule: AutoCategorizationRule) -> str:
    pair = _type_and_category(rule.category)
    where = f"{pair[0].name} › {pair[1].name}" if pair else rule.category.name
    return f"«{rule.match_pattern}»{movement_note(_txn_type_value(rule))} → {where}"


_NATURE_TEXT = {"fijo": "fijo", "traspaso": "traspaso interno"}


def plan_import(session: Session, user_id: int, lib: LibraryFile, mode: str = MODE_MERGE,
                allow_global: bool = False) -> ImportPlan:
    """What apply_import would change, without writing anything."""
    if mode not in (MODE_MERGE, MODE_REPLACE):
        raise LibraryError("Modo de importación no válido.")
    state = _load_state(session, user_id)
    plan = ImportPlan(mode=mode)
    if not lib.checksum_ok:
        plan.warnings.append("La huella del archivo no coincide: se editó a mano o está dañado. "
                             "Revisa la vista previa antes de importar.")

    for lib_type in lib.types:
        if _fold(lib_type.name) not in state.types:
            plan.new_types.append(lib_type.name)
        for cat in lib_type.categories:
            if (_fold(lib_type.name), _fold(cat)) not in state.categories:
                plan.new_categories.append(f"{lib_type.name} › {cat}")
        for cat, nature in lib_type.natures:
            existing = state.categories.get((_fold(lib_type.name), _fold(cat)))
            if existing is None or existing.nature is None:
                plan.nature_changes.append(f"{lib_type.name} › {cat}: {_NATURE_TEXT[nature]}")

    file_personal: Dict[tuple, LibRule] = {}
    file_global: set = set()
    for rule in lib.rules:
        key = _rule_key(rule.type, rule.category, rule.pattern, rule.match_type, rule.transaction_type)
        if rule.scope == SCOPE_GLOBAL:
            if not allow_global:
                plan.skipped_global += 1
            elif key in file_global:
                continue
            elif key in state.global_:
                plan.unchanged_rules += 1
            else:
                plan.new_rules.append(rule)
            file_global.add(key)
            continue
        if key in file_personal:
            continue
        file_personal[key] = rule
        existing = state.personal.get(key)
        if existing is None:
            plan.new_rules.append(rule)
        elif mode == MODE_REPLACE and existing.priority != rule.priority:
            plan.priority_changes.append((rule, existing.priority))
        else:
            plan.unchanged_rules += 1

    if mode == MODE_REPLACE:
        plan.deleted_rules = [_describe(r) for k, r in state.personal.items() if k not in file_personal]
        plan.deleted_rules += [_describe(r) for r in state.stray_personal]

    if plan.skipped_global:
        plan.warnings.append(f"{plan.skipped_global} reglas globales del archivo no se importan aquí: "
                             "las reglas globales solo se cambian desde la aplicación del PC.")
    return plan


@dataclass
class ImportResult:
    types_created: int = 0
    categories_created: int = 0
    natures_set: int = 0
    rules_created: int = 0
    rules_updated: int = 0
    rules_deleted: int = 0


def apply_import(session: Session, user_id: int, lib: LibraryFile, mode: str = MODE_MERGE,
                 allow_global: bool = False) -> ImportResult:
    """Apply the import in one transaction (all or nothing).

    The plan is recomputed against the current database, so a preview made
    earlier (web token, open dialog) can never apply stale decisions.
    """
    plan = plan_import(session, user_id, lib, mode, allow_global)
    result = ImportResult()
    now = datetime.now(timezone.utc)
    try:
        state = _load_state(session, user_id)

        for lib_type in lib.types:
            type_node = state.types.get(_fold(lib_type.name))
            if type_node is None:
                type_node = Category(
                    user_id=user_id, parent_id=None, name=lib_type.name, level=CategoryLevel.type,
                    transaction_type=TransactionType(lib_type.transaction_type) if lib_type.transaction_type else None,
                    created_at=now,
                )
                session.add(type_node)
                session.flush()
                state.types[_fold(lib_type.name)] = type_node
                result.types_created += 1
            for cat_name in lib_type.categories:
                key = (_fold(lib_type.name), _fold(cat_name))
                if key in state.categories:
                    continue
                cat = Category(user_id=user_id, parent_id=type_node.id, name=cat_name,
                               level=CategoryLevel.category, transaction_type=None, created_at=now)
                session.add(cat)
                session.flush()
                state.categories[key] = cat
                result.categories_created += 1
            for cat_name, nature in lib_type.natures:     # only where none is set yet
                cat = state.categories.get((_fold(lib_type.name), _fold(cat_name)))
                if cat is not None and cat.nature is None:
                    cat.nature = nature
                    result.natures_set += 1

        for rule in plan.new_rules:
            cat = state.categories[(_fold(rule.type), _fold(rule.category))]
            session.add(AutoCategorizationRule(
                user_id=None if rule.scope == SCOPE_GLOBAL else user_id,
                category_id=cat.id, match_pattern=rule.pattern,
                match_type=MatchType(rule.match_type), priority=rule.priority, created_at=now,
                transaction_type=TransactionType(rule.transaction_type) if rule.transaction_type else None,
            ))
            result.rules_created += 1

        for rule, _old in plan.priority_changes:
            existing = state.personal[_rule_key(rule.type, rule.category, rule.pattern, rule.match_type,
                                                rule.transaction_type)]
            existing.priority = rule.priority
            result.rules_updated += 1

        if mode == MODE_REPLACE:
            keep = {_rule_key(r.type, r.category, r.pattern, r.match_type, r.transaction_type)
                    for r in lib.rules if r.scope == SCOPE_PERSONAL}
            doomed = [r for k, r in state.personal.items() if k not in keep] + state.stray_personal
            for rule in doomed:
                session.delete(rule)
                result.rules_deleted += 1

        session.commit()
    except Exception:
        session.rollback()
        raise
    return result
