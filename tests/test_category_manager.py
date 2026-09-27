"""
tests/test_category_manager.py
===============================
First-run seeding and category CRUD — what "Cargar categorías por defecto"
and the Categorías screen rely on.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db.database import _set_sqlite_pragma

from app.auth.auth_service import create_user
from app.categorization.category_manager import (
    assign_transaction_category,
    create_category,
    delete_category,
    get_all_categories,
    seed_default_categories,
    seed_default_rules,
    update_category_name,
)
from app.db.models import (
    CATEGORY_SOURCE_MANUAL, Account, AutoCategorizationRule, Base, Category,
    CategoryLevel, Transaction, TransactionType,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    # Same connection setup as the app: ON DELETE SET NULL / CASCADE need
    # PRAGMA foreign_keys=ON.
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def user_id(session):
    return create_user(session, "ana", "secreto1").id


def test_seeding_is_idempotent_and_per_user(session, user_id):
    created = seed_default_categories(session, user_id)
    rules = seed_default_rules(session, user_id)
    assert created > 0 and rules > 0

    assert seed_default_categories(session, user_id) == 0
    assert seed_default_rules(session, user_id) == 0

    types = {c.name for c in get_all_categories(session, user_id, level=CategoryLevel.type)}
    assert types == {"GASTO", "INGRESO", "SERV. BANCARIO", "INVERSION"}
    other = create_user(session, "pedro", "secreto1").id
    assert get_all_categories(session, other) == []


def test_seeded_rules_point_to_the_users_own_categories(session, user_id):
    seed_default_categories(session, user_id)
    seed_default_rules(session, user_id)
    for rule in session.query(AutoCategorizationRule).all():
        assert rule.user_id == user_id
        assert rule.category.user_id == user_id
        assert rule.category.level == CategoryLevel.category


def test_duplicate_names_resolve_to_the_right_type(session, user_id):
    seed_default_categories(session, user_id)
    seed_default_rules(session, user_id)
    hbo_rule = (
        session.query(AutoCategorizationRule)
        .filter(AutoCategorizationRule.match_pattern == "HBO").one()
    )
    assert hbo_rule.category.parent.name == "GASTO"


def test_delete_category_uncategorizes_transactions_and_removes_rules(session, user_id):
    seed_default_categories(session, user_id)
    seed_default_rules(session, user_id)
    luz = next(c for c in get_all_categories(session, user_id) if c.name == "Luz")
    account = Account(user_id=user_id, account_name="Cuenta")
    session.add(account)
    session.flush()
    txn = Transaction(user_id=user_id, account_id=account.id,
                      transaction_date=date(2026, 9, 1), amount=-40.0,
                      type=TransactionType.expense, category_id=luz.id)
    session.add(txn)
    session.commit()

    delete_category(session, luz.id)

    session.refresh(txn)
    assert txn.category_id is None
    assert session.query(AutoCategorizationRule).filter_by(category_id=luz.id).count() == 0


def test_create_rename_and_validation(session, user_id):
    gasto = create_category(session, user_id, "GASTO", CategoryLevel.type,
                            transaction_type=TransactionType.expense)
    cat = create_category(session, user_id, "  Mascotas ", CategoryLevel.category, parent_id=gasto.id)
    assert cat.name == "Mascotas"
    assert update_category_name(session, cat.id, "Veterinario").name == "Veterinario"
    with pytest.raises(ValueError):
        create_category(session, user_id, "Sin padre", CategoryLevel.category)
    with pytest.raises(ValueError):
        update_category_name(session, cat.id, "   ")


def test_manual_assignment_is_marked_and_clearing_resets_source(session, user_id):
    gasto = create_category(session, user_id, "GASTO", CategoryLevel.type)
    cat = create_category(session, user_id, "Varios", CategoryLevel.category, parent_id=gasto.id)
    account = Account(user_id=user_id, account_name="Cuenta")
    session.add(account)
    session.flush()
    txn = Transaction(user_id=user_id, account_id=account.id,
                      transaction_date=date(2026, 9, 1), amount=-1.0,
                      type=TransactionType.expense)
    session.add(txn)
    session.commit()

    assign_transaction_category(session, txn.id, cat.id)
    assert (txn.category_id, txn.category_source) == (cat.id, CATEGORY_SOURCE_MANUAL)
    assign_transaction_category(session, txn.id, None)
    assert (txn.category_id, txn.category_source) == (None, None)
    with pytest.raises(ValueError):
        assign_transaction_category(session, 999_999, cat.id)
