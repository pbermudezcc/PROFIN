"""
tests/test_period_summary.py
=============================
SQL-side aggregates behind the Gastos / Ingresos views.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analytics.period_summary import (
    UNCATEGORIZED_LABEL,
    category_totals,
    period_totals,
    summarize_period,
)
from app.auth.auth_service import create_user
from app.db.models import (
    Account, Base, Category, CategoryLevel, Transaction, TransactionType,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def family(db_session):
    ana = create_user(db_session, "ana", "secreto1")
    otro = create_user(db_session, "otro", "secreto1")
    accounts = {}
    for user in (ana, otro):
        account = Account(user_id=user.id, account_name="Cuenta")
        db_session.add(account)
        db_session.flush()
        accounts[user.id] = account.id
    gasto = Category(user_id=ana.id, name="GASTO", level=CategoryLevel.type,
                     transaction_type=TransactionType.expense)
    db_session.add(gasto)
    db_session.flush()
    super_ = Category(user_id=ana.id, parent_id=gasto.id, name="Supermercados",
                      level=CategoryLevel.category)
    luz = Category(user_id=ana.id, parent_id=gasto.id, name="Luz",
                   level=CategoryLevel.category)
    db_session.add_all([super_, luz])
    db_session.commit()

    def add(user, day, amount, category=None, issuer="X"):
        db_session.add(Transaction(
            user_id=user.id, account_id=accounts[user.id],
            transaction_date=day, amount=amount,
            type=TransactionType.expense if amount < 0 else TransactionType.income,
            category_id=category.id if category else None, issuer_name=issuer,
        ))

    add(ana, date(2026, 8, 30), -10.10, super_, "MERCADONA")
    add(ana, date(2026, 9, 1), -20.20, super_, "LIDL")
    add(ana, date(2026, 9, 1), -30.00, luz, "IBERDROLA")
    add(ana, date(2026, 9, 15), -5.05, None, "BAR")
    add(ana, date(2026, 9, 15), 1500.00, None, "NOMINA")   # income, excluded
    add(otro, date(2026, 9, 1), -999.0, None, "OTHER USER")  # other user, excluded
    db_session.commit()
    return ana


def test_summarize_period_totals_and_kpis(db_session, family):
    summary = summarize_period(
        db_session, family.id, TransactionType.expense,
        date(2026, 8, 1), date(2026, 9, 30),
    )
    assert summary.total == 65.35
    assert summary.count == 4
    assert summary.max_amount == 30.0
    assert summary.categorized == 3
    assert summary.average == round(65.35 / 4, 2)


def test_summarize_period_groupings(db_session, family):
    summary = summarize_period(
        db_session, family.id, TransactionType.expense,
        date(2026, 8, 1), date(2026, 9, 30),
    )
    assert summary.by_category == [
        ("Supermercados", 30.3), ("Luz", 30.0), (UNCATEGORIZED_LABEL, 5.05),
    ]
    assert [month for month, _ in summary.by_month_category] == [
        date(2026, 8, 1), date(2026, 9, 1),
    ]
    assert summary.by_month_category[1][1][0] == ("Luz", 30.0)
    # Chronological across months (was sorted as "dd/mm" text before).
    assert [day for day, _ in summary.by_day] == [
        date(2026, 8, 30), date(2026, 9, 1), date(2026, 9, 15),
    ]
    assert summary.by_day[1] == (date(2026, 9, 1), 50.2)
    assert [row.description for row in summary.latest] == [
        "BAR", "IBERDROLA", "LIDL", "MERCADONA",
    ]
    assert summary.latest[0].category is None


def test_period_totals_and_category_totals_for_income(db_session, family):
    assert period_totals(
        db_session, family.id, TransactionType.income,
        date(2026, 9, 1), date(2026, 9, 30),
    ) == (1500.0, 1)
    assert category_totals(
        db_session, family.id, TransactionType.income,
        date(2026, 9, 1), date(2026, 9, 30),
    ) == [(UNCATEGORIZED_LABEL, 1500.0)]


def test_empty_period(db_session, family):
    summary = summarize_period(
        db_session, family.id, TransactionType.expense,
        date(2020, 1, 1), date(2020, 1, 31),
    )
    assert (summary.total, summary.count, summary.by_day, summary.latest) == (0.0, 0, [], [])
    assert summary.categorized_pct == 0.0
