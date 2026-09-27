"""
tests/test_investments_ledger.py
================================
Writes to the investment ledger: create/edit/delete with replay checks,
per-user scoping, field normalization, filters, brokers and the EUR
estimate used by the entry form.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db.database import _build_engine
from app.db.models import Base, InvestmentKind, InvestmentTransaction, Security, User
from app.investments.ledger import (
    DEFAULT_BROKERS,
    TransactionDraft,
    delete_transaction,
    estimate_amount_eur,
    known_brokers,
    list_transactions,
    picker_securities,
    save_transaction,
)
from app.investments.portfolio import LedgerError

BUY, SELL, DIV, SPLIT = (InvestmentKind.buy, InvestmentKind.sell,
                         InvestmentKind.dividend, InvestmentKind.split)


@pytest.fixture
def db(tmp_path):
    engine = _build_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    pedro = User(username="pedro", password_hash="x")
    maria = User(username="maria", password_hash="x")
    gev = Security(symbol="GEV", name="GE Vernova Inc.", currency="USD")
    enr = Security(symbol="ENR.DE", name="Siemens Energy AG", currency="EUR")
    amz = Security(symbol="AMZ.DE", name="Amazon.com, Inc.", currency="EUR")
    session.add_all([pedro, maria, gev, enr, amz])
    session.commit()
    yield SimpleNamespace(s=session, pedro=pedro, maria=maria, gev=gev, enr=enr, amz=amz)
    session.close()
    engine.dispose()


def draft(sec, kind, d, qty=None, eur=0.0, **kw):
    kw.setdefault("broker", "DEGIRO")
    return TransactionDraft(security_id=sec.id, kind=kind, trade_date=d,
                            quantity=qty, amount_eur=eur, **kw)


def test_save_new_buy_and_sell(db):
    save_transaction(db.s, db.pedro.id, draft(db.gev, BUY, date(2025, 3, 3), 2, 575.4, price=300.0))
    save_transaction(db.s, db.pedro.id, draft(db.gev, SELL, date(2026, 3, 3), 1, 700.0))
    assert db.s.query(InvestmentTransaction).count() == 2


def test_oversell_is_rejected_and_nothing_is_written(db):
    save_transaction(db.s, db.pedro.id, draft(db.gev, BUY, date(2025, 3, 3), 2, 575.4))
    with pytest.raises(LedgerError, match="solo había 2"):
        save_transaction(db.s, db.pedro.id, draft(db.gev, SELL, date(2026, 3, 3), 3, 900.0))
    assert db.s.query(InvestmentTransaction).count() == 1


def test_holdings_are_per_user(db):
    save_transaction(db.s, db.maria.id, draft(db.gev, BUY, date(2025, 3, 3), 5, 1500.0))
    with pytest.raises(LedgerError):
        save_transaction(db.s, db.pedro.id, draft(db.gev, SELL, date(2026, 1, 1), 1, 300.0))


def test_editing_a_buy_that_breaks_a_later_sale_is_rejected(db):
    buy = save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 10, 1000.0))
    save_transaction(db.s, db.pedro.id, draft(db.enr, SELL, date(2025, 6, 1), 8, 1200.0))
    edit = draft(db.enr, BUY, date(2025, 1, 2), 5, 500.0, id=buy.id)
    with pytest.raises(LedgerError):
        save_transaction(db.s, db.pedro.id, edit)
    db.s.refresh(buy)
    assert buy.quantity == 10


def test_moving_a_buy_to_another_security_checks_the_old_one(db):
    buy = save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 10, 1000.0))
    save_transaction(db.s, db.pedro.id, draft(db.enr, SELL, date(2025, 6, 1), 8, 1200.0))
    with pytest.raises(LedgerError):
        save_transaction(db.s, db.pedro.id,
                         draft(db.amz, BUY, date(2025, 1, 2), 10, 1000.0, id=buy.id))


def test_editing_updates_in_place(db):
    buy = save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 10, 1000.0))
    save_transaction(db.s, db.pedro.id,
                     draft(db.enr, BUY, date(2025, 1, 3), 12, 1234.567, id=buy.id, notes="  x "))
    rows = db.s.query(InvestmentTransaction).all()
    assert len(rows) == 1
    assert (rows[0].quantity, rows[0].amount_eur, rows[0].trade_date, rows[0].notes) == (
        12, 1234.57, date(2025, 1, 3), "x")


def test_cannot_edit_or_delete_another_users_operation(db):
    buy = save_transaction(db.s, db.maria.id, draft(db.enr, BUY, date(2025, 1, 2), 10, 1000.0))
    with pytest.raises(LedgerError, match="otro usuario"):
        save_transaction(db.s, db.pedro.id,
                         draft(db.enr, BUY, date(2025, 1, 2), 1, 1.0, id=buy.id))
    with pytest.raises(LedgerError, match="otro usuario"):
        delete_transaction(db.s, db.pedro.id, buy.id)


def test_delete_is_blocked_when_a_later_sale_depends_on_it(db):
    buy = save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 10, 1000.0))
    sell = save_transaction(db.s, db.pedro.id, draft(db.enr, SELL, date(2025, 6, 1), 8, 1200.0))
    with pytest.raises(LedgerError, match="No se puede borrar"):
        delete_transaction(db.s, db.pedro.id, buy.id)
    delete_transaction(db.s, db.pedro.id, sell.id)
    delete_transaction(db.s, db.pedro.id, buy.id)
    assert db.s.query(InvestmentTransaction).count() == 0


def test_invalid_fields_and_missing_broker(db):
    with pytest.raises(LedgerError, match="bróker"):
        save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 1, 10.0, broker=" "))
    with pytest.raises(LedgerError, match="acciones"):
        save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 0, 10.0))


def test_normalization_clears_fields_the_kind_does_not_use(db):
    save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 10, 1000.0,
                                              tax_withheld_eur=5.0, split_ratio=3.0))
    save_transaction(db.s, db.pedro.id, draft(db.enr, DIV, date(2025, 3, 2), 10, 8.5,
                                              price=1.0, tax_withheld_eur=1.5))
    save_transaction(db.s, db.pedro.id, draft(db.enr, SPLIT, date(2025, 4, 2), 10, 99.0,
                                              split_ratio=2.0))
    buy, div, split = db.s.query(InvestmentTransaction).order_by(InvestmentTransaction.trade_date)
    assert (buy.tax_withheld_eur, buy.split_ratio) == (0.0, None)
    assert (div.quantity, div.price, div.tax_withheld_eur) == (None, None, 1.5)
    assert (split.quantity, split.amount_eur, split.split_ratio) == (None, 0.0, 2.0)


def test_list_filters_and_order(db):
    save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 10, 1000.0))
    save_transaction(db.s, db.pedro.id, draft(db.gev, BUY, date(2025, 2, 2), 1, 300.0,
                                              broker="EquatePlus"))
    save_transaction(db.s, db.maria.id, draft(db.amz, BUY, date(2025, 3, 2), 1, 200.0))
    rows = list_transactions(db.s, db.pedro.id)
    assert [r.security.symbol for r in rows] == ["GEV", "ENR.DE"]
    assert [r.security.symbol for r in list_transactions(db.s, db.pedro.id, text="siemens")] == ["ENR.DE"]
    assert [r.broker for r in list_transactions(db.s, db.pedro.id, broker="EquatePlus")] == ["EquatePlus"]
    assert list_transactions(db.s, db.pedro.id, kind=SELL) == []


def test_known_brokers_most_recent_first_then_defaults(db):
    save_transaction(db.s, db.pedro.id, draft(db.enr, BUY, date(2025, 1, 2), 1, 10.0, broker="Trade Republic"))
    save_transaction(db.s, db.pedro.id, draft(db.gev, BUY, date(2025, 5, 2), 1, 10.0, broker="EquatePlus"))
    brokers = known_brokers(db.s, db.pedro.id)
    assert brokers[:2] == ["EquatePlus", "Trade Republic"]
    assert set(DEFAULT_BROKERS) <= set(brokers) and len(brokers) == len(set(brokers))


def test_picker_lists_own_securities_first(db):
    save_transaction(db.s, db.pedro.id, draft(db.gev, BUY, date(2025, 1, 2), 1, 10.0))
    assert [s.symbol for s in picker_securities(db.s, db.pedro.id)] == ["GEV", "AMZ.DE", "ENR.DE"]


@pytest.mark.parametrize("kind, fx, expected", [
    (BUY, 1.0, 1002.5),          # EUR security: exact
    (SELL, 1.0, 997.5),
    (BUY, 1.25, 802.5),          # USD: 1000 USD / 1.25 + fees
    (BUY, None, None),
])
def test_estimate_amount_eur(kind, fx, expected):
    assert estimate_amount_eur(kind, 10, 100.0, 2.5, fx) == expected
