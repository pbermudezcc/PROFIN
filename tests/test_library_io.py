"""
tests/test_library_io.py
========================
Export / import of the categorization library (categories + rules):
round trip, idempotency, merge vs replace, global rules, validation of
hostile or damaged files, and all-or-nothing apply.
"""
from __future__ import annotations

import gzip
import json
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.auth.auth_service import create_user
from app.categorization import library_io as lio
from app.categorization.category_manager import seed_default_categories, seed_default_rules
from app.db.database import _set_sqlite_pragma
from app.db.models import (
    CATEGORY_SOURCE_MANUAL, Account, AutoCategorizationRule, Base, Category,
    CategoryLevel, MatchType, Transaction, TransactionType,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def ana(session):
    return create_user(session, "ana", "clave-secreta-1").id


@pytest.fixture
def beto(session):
    return create_user(session, "beto", "clave-secreta-2").id


def _cat(session, user_id, type_name, name):
    type_node = session.query(Category).filter_by(user_id=user_id, name=type_name,
                                                  level=CategoryLevel.type).one()
    return session.query(Category).filter_by(user_id=user_id, parent_id=type_node.id, name=name).one()


def _add_rule(session, user_id, category, pattern, kind=MatchType.contains, priority=100):
    rule = AutoCategorizationRule(user_id=user_id, category_id=category.id, match_pattern=pattern,
                                  match_type=kind, priority=priority)
    session.add(rule)
    session.commit()
    return rule


def _snapshot(session, user_id):
    """Tree and personal rules by natural key — what a round trip must keep."""
    cats = sorted((c.parent.name if c.parent else "", c.name, c.level.value)
                  for c in session.query(Category).filter_by(user_id=user_id))
    rules = sorted((r.category.parent.name, r.category.name, r.match_pattern, r.match_type.value, r.priority)
                   for r in session.query(AutoCategorizationRule).filter_by(user_id=user_id))
    return cats, rules


def _roundtrip(session, user_id, **kw):
    return lio.parse_library(lio.dumps(lio.export_library(session, user_id, **kw)))


@pytest.fixture
def seeded(session, ana):
    seed_default_categories(session, ana)
    seed_default_rules(session, ana)
    return ana


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_roundtrip_into_empty_user_reproduces_tree_and_rules(session, seeded, beto):
    lib = _roundtrip(session, seeded)
    assert lib.checksum_ok
    result = lio.apply_import(session, beto, lib)
    assert result.categories_created > 0 and result.rules_created > 0
    assert _snapshot(session, beto) == _snapshot(session, seeded)


def test_export_is_deterministic_apart_from_timestamp(session, seeded):
    first = json.loads(lio.dumps(lio.export_library(session, seeded)))
    second = json.loads(lio.dumps(lio.export_library(session, seeded)))
    for payload in (first, second):
        payload.pop("exported_at")
        payload.pop("checksum")
    assert first == second


def test_export_has_no_ids_and_carries_versions(session, seeded):
    payload = lio.export_library(session, seeded, source_user="ana")
    assert payload["format"] == lio.FORMAT_NAME and payload["format_version"] == lio.FORMAT_VERSION
    assert payload["app_version"] and payload["db_schema_version"] >= 1
    assert '"id"' not in lio.dumps(payload).decode()


def test_import_twice_changes_nothing_the_second_time(session, seeded, beto):
    lib = _roundtrip(session, seeded)
    lio.apply_import(session, beto, lib)
    plan = lio.plan_import(session, beto, lib)
    assert not plan.has_changes
    result = lio.apply_import(session, beto, lib)
    assert (result.categories_created, result.rules_created) == (0, 0)


def test_same_category_name_under_two_types_is_kept_apart(session, ana, beto):
    for type_name, tx in (("GASTO", TransactionType.expense), ("INGRESO", TransactionType.income)):
        root = Category(user_id=ana, name=type_name, level=CategoryLevel.type, transaction_type=tx)
        session.add(root)
        session.flush()
        session.add(Category(user_id=ana, parent_id=root.id, name="Hipoteca", level=CategoryLevel.category))
    session.commit()
    _add_rule(session, ana, _cat(session, ana, "INGRESO", "Hipoteca"), "DEVOLUCION HIPOTECA")

    lio.apply_import(session, beto, _roundtrip(session, ana))
    rule = session.query(AutoCategorizationRule).filter_by(user_id=beto).one()
    assert rule.category.parent.name == "INGRESO"


# ---------------------------------------------------------------------------
# Merge / replace
# ---------------------------------------------------------------------------

@pytest.fixture
def two_libraries(session, ana, beto):
    """ana: GASTO › Super (MERCADONA 100, LIDL 100). beto: GASTO › Super (MERCADONA 50, ALDI)."""
    for uid in (ana, beto):
        root = Category(user_id=uid, name="GASTO", level=CategoryLevel.type,
                        transaction_type=TransactionType.expense)
        session.add(root)
        session.flush()
        session.add(Category(user_id=uid, parent_id=root.id, name="Super", level=CategoryLevel.category))
    session.commit()
    _add_rule(session, ana, _cat(session, ana, "GASTO", "Super"), "MERCADONA")
    _add_rule(session, ana, _cat(session, ana, "GASTO", "Super"), "LIDL")
    _add_rule(session, beto, _cat(session, beto, "GASTO", "Super"), "mercadona", priority=50)
    _add_rule(session, beto, _cat(session, beto, "GASTO", "Super"), "ALDI")
    return _roundtrip(session, ana)


def _patterns(session, user_id):
    return {(r.match_pattern.upper(), r.priority)
            for r in session.query(AutoCategorizationRule).filter_by(user_id=user_id)}


def test_merge_only_adds(session, beto, two_libraries):
    plan = lio.plan_import(session, beto, two_libraries, lio.MODE_MERGE)
    assert [r.pattern for r in plan.new_rules] == ["LIDL"]
    assert not plan.deleted_rules and not plan.priority_changes
    lio.apply_import(session, beto, two_libraries, lio.MODE_MERGE)
    assert _patterns(session, beto) == {("MERCADONA", 50), ("ALDI", 100), ("LIDL", 100)}


def test_replace_makes_personal_rules_equal_to_the_file(session, beto, two_libraries):
    plan = lio.plan_import(session, beto, two_libraries, lio.MODE_REPLACE)
    assert len(plan.deleted_rules) == 1 and "ALDI" in plan.deleted_rules[0]
    assert [(r.pattern, old) for r, old in plan.priority_changes] == [("MERCADONA", 50)]
    result = lio.apply_import(session, beto, two_libraries, lio.MODE_REPLACE)
    assert (result.rules_created, result.rules_updated, result.rules_deleted) == (1, 1, 1)
    assert _patterns(session, beto) == {("MERCADONA", 100), ("LIDL", 100)}


def test_replace_never_deletes_categories_or_touches_transactions(session, beto, two_libraries):
    root = session.query(Category).filter_by(user_id=beto, name="GASTO").one()
    extra = Category(user_id=beto, parent_id=root.id, name="Solo de Beto", level=CategoryLevel.category)
    session.add(extra)
    account = Account(user_id=beto, account_name="Cuenta")
    session.add(account)
    session.flush()
    txn = Transaction(user_id=beto, account_id=account.id, transaction_date=date(2026, 1, 2),
                      concept="ALDI", amount=-5.0, type=TransactionType.expense,
                      category_id=extra.id, category_source=CATEGORY_SOURCE_MANUAL)
    session.add(txn)
    session.commit()

    lio.apply_import(session, beto, two_libraries, lio.MODE_REPLACE)
    session.refresh(txn)
    assert session.get(Category, extra.id) is not None
    assert (txn.category_id, txn.category_source) == (extra.id, CATEGORY_SOURCE_MANUAL)


# ---------------------------------------------------------------------------
# Global rules
# ---------------------------------------------------------------------------

def test_global_rules_need_both_flags(session, ana, beto, two_libraries):
    _add_rule(session, None, _cat(session, ana, "GASTO", "Super"), "CARREFOUR")
    without = _roundtrip(session, ana)
    assert all(r.scope == lio.SCOPE_PERSONAL for r in without.rules)

    lib = _roundtrip(session, ana, include_global=True)
    assert any(r.scope == lio.SCOPE_GLOBAL for r in lib.rules)

    # The existing global rule is recognized, not duplicated.
    plan = lio.plan_import(session, beto, lib, allow_global=True)
    assert all(r.scope == lio.SCOPE_PERSONAL for r in plan.new_rules)

    session.query(AutoCategorizationRule).filter_by(user_id=None).delete()
    session.commit()
    plan = lio.plan_import(session, beto, lib, allow_global=False)
    assert plan.skipped_global == 1 and plan.warnings
    lio.apply_import(session, beto, lib, allow_global=False)
    assert session.query(AutoCategorizationRule).filter_by(user_id=None).count() == 0

    lio.apply_import(session, beto, lib, allow_global=True)
    rule = session.query(AutoCategorizationRule).filter_by(user_id=None).one()
    assert rule.category.user_id == beto


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _valid_payload():
    return {
        "format": lio.FORMAT_NAME, "format_version": 1,
        "types": [{"name": "GASTO", "transaction_type": "expense", "categories": ["Super"]}],
        "rules": [{"type": "GASTO", "category": "Super", "pattern": "LIDL",
                   "match_type": "contains", "priority": 100, "scope": "personal"}],
    }


def test_valid_payload_parses_and_gzip_is_accepted():
    raw = lio.dumps(_valid_payload())
    assert lio.parse_library(raw).checksum_ok
    assert lio.parse_library(gzip.compress(raw)).rules[0].pattern == "LIDL"


def test_hand_edited_file_is_flagged_not_rejected():
    payload = json.loads(lio.dumps(_valid_payload()))
    payload["rules"][0]["pattern"] = "ALDI"
    lib = lio.parse_library(json.dumps(payload).encode())
    assert not lib.checksum_ok and lib.rules[0].pattern == "ALDI"


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.update(format="otra.cosa"), "biblioteca"),
    (lambda p: p.update(format_version=99), "versión de formato 99"),
    (lambda p: p["rules"][0].update(match_type="regex", pattern="(sin cerrar"), "regular"),
    (lambda p: p["rules"][0].update(priority=0), "Prioridad"),
    (lambda p: p["rules"][0].update(priority=True), "Prioridad"),
    (lambda p: p["rules"][0].update(category="Inexistente"), "no está en el archivo"),
    (lambda p: p["rules"][0].update(scope="todos"), "Ámbito"),
    (lambda p: p["types"][0].update(transaction_type="robo"), "desconocido"),
    (lambda p: p["types"][0].update(name="x" * 200), "supera"),
    (lambda p: p["types"].append(dict(p["types"][0])), "dos veces"),
    (lambda p: p.pop("rules"), "secciones"),
])
def test_invalid_files_are_rejected_with_a_clear_message(mutate, message):
    payload = _valid_payload()
    mutate(payload)
    with pytest.raises(lio.LibraryError, match=message):
        lio.parse_library(json.dumps(payload).encode())


def test_garbage_oversized_and_gzip_bomb_are_rejected():
    with pytest.raises(lio.LibraryError, match="JSON"):
        lio.parse_library(b"\x00\x01not json")
    with pytest.raises(lio.LibraryError, match="grande"):
        lio.parse_library(b" " * (lio.MAX_FILE_BYTES + 1))
    bomb = gzip.compress(b" " * (lio.MAX_FILE_BYTES * 4))
    assert len(bomb) < lio.MAX_FILE_BYTES
    with pytest.raises(lio.LibraryError, match="grande"):
        lio.parse_library(bomb)


def test_older_format_is_upgraded(monkeypatch):
    payload = _valid_payload()
    monkeypatch.setattr(lio, "FORMAT_VERSION", 2)
    monkeypatch.setitem(lio._UPGRADES, 1, lambda p: {**p, "format_version": 2})
    assert lio.parse_library(json.dumps(payload).encode()).rules[0].pattern == "LIDL"


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

def test_failure_half_way_leaves_the_database_unchanged(session, beto, two_libraries, monkeypatch):
    before = _snapshot(session, beto)
    real_add = session.add
    calls = {"n": 0}

    def flaky_add(obj):
        if isinstance(obj, AutoCategorizationRule):
            raise RuntimeError("disco lleno")
        calls["n"] += 1
        return real_add(obj)

    lib = lio.LibraryFile(types=list(two_libraries.types) + [lio.LibType("NUEVO", None, ("X",))],
                          rules=two_libraries.rules)
    monkeypatch.setattr(session, "add", flaky_add)
    with pytest.raises(RuntimeError):
        lio.apply_import(session, beto, lib)
    monkeypatch.undo()
    assert calls["n"] > 0          # categories were added before the failure…
    assert _snapshot(session, beto) == before   # …and rolled back with it


def test_suggested_filename_is_safe():
    name = lio.suggested_filename("Ana María/../x", when=date(2026, 10, 9))
    assert name == "profin-categorias-Ana_Mar_a_x-20261009.profin-cats.json"
