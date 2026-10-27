"""
tests/test_investments_cash.py
==============================
Cash left at the brokers (broker_cash): read from the DEGIRO statement's
«Saldo», stored by the import (never overwritten by an older statement),
and added to the portfolio's total value and allocation.
"""
from __future__ import annotations

from datetime import date

import pytest
from openpyxl import Workbook

from app.db.models import BrokerCash, FxRate
from app.investments.cash import cash_changes, save_statement_cash, user_cash
from app.investments.importers.degiro import parse_statement
from app.investments.performance import CASH_LABEL, allocation
from app.investments.snapshot import portfolio_snapshot
from tests.test_investments_import import HEADER, ROWS, _as_values, db, run_import  # noqa: F401
from tests.test_investments_snapshot import AS_OF, B, op
from tests.test_investments_snapshot import db as snap_db  # noqa: F401


def write_with_balances(path, rows, balances):
    """Like write_xlsx (newest first) but with a «Saldo» per row: balances[i]
    is (currency, amount) for rows[i] (oldest first)."""
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for row, (currency, amount) in reversed(list(zip(rows, balances))):
        values = _as_values(row)
        values[9], values[10] = currency, amount
        ws.append(values)
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Reading the statement
# ---------------------------------------------------------------------------

def test_newest_balance_per_currency_is_the_cash(tmp_path):
    balances = [("EUR", 100.0 + i) for i in range(len(ROWS))]
    balances[-3] = ("USD", 7.5)                    # the last USD balance (before the sale)
    statement = parse_statement(write_with_balances(tmp_path / "a.xlsx", ROWS, balances))
    assert statement.cash == {"EUR": 100.0 + len(ROWS) - 1, "USD": 7.5}
    assert statement.cash_as_of == date(2026, 3, 3)


def test_within_the_same_minute_the_first_row_of_the_file_is_the_newest(tmp_path):
    # Real DEGIRO export: a sweep and the transfer row share 15:31; the file
    # lists the transfer first and its «Saldo» is the cash DEGIRO shows.
    rows = [("26-10-2026", "15:31", None, None, "Degiro Cash Sweep Transfer", None, "EUR", 709.71),
            ("26-10-2026", "15:31", None, None, "Transferir desde su Cuenta de Efectivo en flatex",
             None, "EUR", None)]
    path = write_with_balances(tmp_path / "b.xlsx", rows, [("EUR", 1712.13), ("EUR", 1002.42)])
    assert parse_statement(path).cash == {"EUR": 1002.42}


# ---------------------------------------------------------------------------
# Storing it
# ---------------------------------------------------------------------------

def test_an_older_statement_never_overwrites_newer_cash(db):  # noqa: F811
    s, uid = db.s, db.pedro.id
    assert save_statement_cash(s, uid, "DEGIRO", {"EUR": 1002.42, "USD": 0.0}, date(2026, 10, 26))
    assert cash_changes(s, uid, "DEGIRO", {"EUR": 355.3}, date(2026, 9, 2)) == {}
    assert not save_statement_cash(s, uid, "DEGIRO", {"EUR": 355.3}, date(2026, 9, 2))
    assert [(c.currency, c.amount) for c in user_cash(s, uid)] == [("EUR", 1002.42)]   # USD 0 hidden
    # A newer one replaces it; the same one changes nothing.
    assert cash_changes(s, uid, "DEGIRO", {"EUR": 50.0}, date(2026, 11, 2)) == {"EUR": (1002.42, 50.0)}
    assert save_statement_cash(s, uid, "DEGIRO", {"EUR": 50.0}, date(2026, 11, 2))
    assert not save_statement_cash(s, uid, "DEGIRO", {"EUR": 50.0}, date(2026, 11, 2))
    assert user_cash(s, db.maria.id) == []                   # per user
    assert s.query(BrokerCash).count() == 2


def test_import_stores_the_cash_even_without_new_operations(db, tmp_path):  # noqa: F811
    balances = [("EUR", 10.0)] * (len(ROWS) - 1) + [("EUR", 1234.56)]
    path = write_with_balances(tmp_path / "a.xlsx", ROWS, balances)
    summary = run_import(db, db.pedro, path)
    assert summary.cash_updated and len(summary.result.added) == 8
    assert user_cash(db.s, db.pedro.id)[0].amount == 1234.56

    balances[-1] = ("EUR", 99.0)                             # same operations, new balance
    path = write_with_balances(tmp_path / "b.xlsx", ROWS, balances)
    statement = parse_statement(path)
    statement.cash_as_of = date(2026, 3, 4)
    from app.investments.importers.service import apply_import
    from tests.test_investments_import import SYMBOLS
    again = apply_import(db.s, db.pedro.id, statement, SYMBOLS, {})
    assert again.result.added == [] and again.cash_updated
    assert user_cash(db.s, db.pedro.id)[0].amount == 99.0


# ---------------------------------------------------------------------------
# Portfolio value
# ---------------------------------------------------------------------------

def test_total_value_adds_the_cash_in_eur(snap_db):  # noqa: F811
    db = snap_db
    op(db, db.pedro, db.enr, B, date(2025, 2, 3), 10, 1000.0)                   # 10×150 = 1500
    save_statement_cash(db.s, db.pedro.id, "DEGIRO", {"EUR": 1002.42, "USD": 25.0}, AS_OF)
    totals = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF).totals
    assert totals.market_value_eur == pytest.approx(1500.0)                     # securities only
    assert totals.cash_eur == pytest.approx(1002.42 + 20.0)                     # 25 USD / 1.25
    assert totals.total_value_eur == pytest.approx(2522.42)
    assert totals.unrealized_gain_eur == pytest.approx(500.0)                   # gains: securities
    assert portfolio_snapshot(db.s, db.maria.id, as_of=AS_OF).totals.cash == []
    assert portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF, broker="Otro").totals.cash_eur == 0

    by_value = dict(allocation(db.s, db.pedro.id, "security", as_of=AS_OF))
    assert by_value[CASH_LABEL] == pytest.approx(1022.42)
    assert dict(allocation(db.s, db.pedro.id, "currency", as_of=AS_OF))["USD"] == pytest.approx(20.0)
    assert dict(allocation(db.s, db.pedro.id, "broker", as_of=AS_OF))["DEGIRO"] == pytest.approx(2522.42)


def test_cash_without_fx_rate_is_reported_not_guessed(snap_db):  # noqa: F811
    db = snap_db
    op(db, db.pedro, db.enr, B, date(2025, 2, 3), 10, 1000.0)
    save_statement_cash(db.s, db.pedro.id, "DEGIRO", {"CHF": 100.0}, AS_OF)
    totals = portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF).totals
    assert totals.cash_eur == 0 and totals.cash_missing_fx == ["CHF"]
    db.s.add(FxRate(currency="CHF", rate_date=AS_OF, rate=0.5))
    db.s.commit()
    assert portfolio_snapshot(db.s, db.pedro.id, as_of=AS_OF).totals.cash_eur == pytest.approx(200.0)
