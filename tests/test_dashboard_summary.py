"""
tests/test_dashboard_summary.py
===============================
«Inversión» on the dashboard (desktop) and Resumen (web): movements in a
category of the investment type count as investment, not as income/expense.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analytics.dashboard_summary import dashboard_summary, investment_category_ids
from app.auth.auth_service import create_user
from app.categorization.category_manager import seed_default_categories
from app.db.models import Account, Base, Category, Transaction, TransactionType

DAY = date(2026, 9, 15)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def ana(session):
    user = create_user(session, "ana", "clave-secreta-1")
    seed_default_categories(session, user.id)
    account = Account(user_id=user.id, account_name="Cuenta")
    session.add(account)
    session.commit()
    return user.id, account.id


def _categories(session, user_id, name):
    return {c.id for c in session.query(Category).filter_by(user_id=user_id, name=name)}


def _category(session, user_id, name):
    (cid,) = _categories(session, user_id, name)
    return cid


def _add(session, user_id, account_id, amount, category_id=None, day=DAY):
    session.add(Transaction(
        user_id=user_id, account_id=account_id, transaction_date=day, amount=amount,
        type=TransactionType.expense if amount < 0 else TransactionType.income,
        issuer_name="X", category_id=category_id,
    ))
    session.commit()


def _summary(session, user_id):
    return dashboard_summary(session, user_id, date(2026, 9, 1), date(2026, 9, 30))


def test_seeded_investment_categories_are_found(session, ana):
    user_id, _ = ana
    ids = investment_category_ids(session, user_id)
    assert _category(session, user_id, "Aporte Broker") in ids
    assert _category(session, user_id, "Tressis") in ids
    assert _category(session, user_id, "INVERSION") in ids
    assert not _categories(session, user_id, "Hipoteca") & ids


def test_broker_transfer_counts_as_investment_not_expense(session, ana):
    user_id, account_id = ana
    broker = _category(session, user_id, "Aporte Broker")
    _add(session, user_id, account_id, -300.0, broker)
    _add(session, user_id, account_id, -50.0)                 # uncategorized: still an expense
    _add(session, user_id, account_id, 1000.0)

    s = _summary(session, user_id)

    assert s.investment == 300.0
    assert s.expense == 50.0
    assert s.income == 1000.0
    assert s.savings_rate == 95.0
    assert [name for name, _ in s.by_category] == ["Sin categorizar (1)"]
    assert [(m.income, m.expense) for m in s.monthly] == [(1000.0, 50.0)]


def test_money_back_from_the_broker_lowers_the_net_investment(session, ana):
    user_id, account_id = ana
    broker = _category(session, user_id, "Aporte Broker")
    _add(session, user_id, account_id, -300.0, broker)
    _add(session, user_id, account_id, 100.0, broker)

    s = _summary(session, user_id)

    assert s.investment == 200.0
    assert s.income == 0.0 and s.expense == 0.0


def test_no_investment_categories_keeps_the_old_figures(session):
    user = create_user(session, "beto", "clave-secreta-2")
    account = Account(user_id=user.id, account_name="Cuenta")
    session.add(account)
    session.commit()
    _add(session, user.id, account.id, -20.0)

    s = _summary(session, user.id)

    assert (s.expense, s.investment) == (20.0, 0.0)
