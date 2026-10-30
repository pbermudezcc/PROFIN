"""
tests/test_fixed_nature.py
==========================
Fijo / variable / traspaso marks: the category default, the movement's own
override, the shared search filter, the library file and the services the
desktop and the web both call.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.analytics.transaction_search import (
    NATURE_FILTER_FIXED,
    NATURE_FILTER_TRANSFER,
    NATURE_FILTER_VARIABLE,
    TransactionFilters,
    search_transactions,
)
from app.categorization import library_io
from app.categorization.admin_service import (
    AdminError,
    NotFound,
    set_category_nature,
    set_natures,
    set_transaction_fixed,
    suggested_natures,
    transfer_peers,
)
from app.db.models import (
    CATEGORY_NATURE_FIXED,
    CATEGORY_NATURE_TRANSFER,
    FIXED_OVERRIDE_FIXED,
    FIXED_OVERRIDE_VARIABLE,
    Account,
    Base,
    Category,
    CategoryLevel,
    Transaction,
    TransactionType,
    User,
    effective_nature,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys = ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def home(db):
    """COMUN (joint account) with Luz, Supermercados and an aporte category,
    plus MARIA, PEDRO and the demo account."""
    users = {name: User(username=name, password_hash="x") for name in ("COMUN", "MARIA", "PEDRO", "DEMO")}
    db.add_all(users.values())
    db.flush()
    comun = users["COMUN"]
    account = Account(user_id=comun.id, account_name="Común")
    gasto = Category(user_id=comun.id, name="GASTO", level=CategoryLevel.type,
                     transaction_type=TransactionType.expense)
    ingreso = Category(user_id=comun.id, name="INGRESO", level=CategoryLevel.type,
                       transaction_type=TransactionType.income)
    db.add_all([account, gasto, ingreso])
    db.flush()
    cats = {
        "luz": Category(user_id=comun.id, parent_id=gasto.id, name="Luz", level=CategoryLevel.category),
        "super": Category(user_id=comun.id, parent_id=gasto.id, name="Supermercados",
                          level=CategoryLevel.category),
        "aporte": Category(user_id=comun.id, parent_id=ingreso.id, name="Aporte Comun María",
                           level=CategoryLevel.category),
    }
    db.add_all(cats.values())
    db.flush()

    def txn(cat, amount, **kw):
        t = Transaction(user_id=comun.id, account_id=account.id, transaction_date=date(2026, 9, 1),
                        amount=amount, type=TransactionType.expense if amount < 0 else TransactionType.income,
                        issuer_name=cat.name.upper(), category_id=cat.id, **kw)
        db.add(t)
        return t

    txns = {"luz": txn(cats["luz"], -80), "super": txn(cats["super"], -120),
            "aporte": txn(cats["aporte"], 900)}
    db.commit()
    return {"users": users, "cats": cats, "txns": txns, "gasto": gasto}


def test_effective_nature_precedence():
    assert effective_nature(None, None) is None
    assert effective_nature(None, CATEGORY_NATURE_FIXED) == CATEGORY_NATURE_FIXED
    assert effective_nature(FIXED_OVERRIDE_VARIABLE, CATEGORY_NATURE_FIXED) is None
    assert effective_nature(FIXED_OVERRIDE_FIXED, None) == CATEGORY_NATURE_FIXED
    # An aporte is never a bill, whatever the movement says.
    assert effective_nature(FIXED_OVERRIDE_FIXED, CATEGORY_NATURE_TRANSFER) == CATEGORY_NATURE_TRANSFER
    assert effective_nature(None, "otro") is None


def test_set_category_nature_and_peer(db, home):
    comun, maria = home["users"]["COMUN"], home["users"]["MARIA"]
    aporte = home["cats"]["aporte"]

    set_category_nature(db, comun.id, aporte.id, CATEGORY_NATURE_TRANSFER, maria.id)
    assert (aporte.nature, aporte.transfer_peer_user_id) == (CATEGORY_NATURE_TRANSFER, maria.id)
    # Any other nature drops the link.
    set_category_nature(db, comun.id, aporte.id, CATEGORY_NATURE_FIXED, maria.id)
    assert (aporte.nature, aporte.transfer_peer_user_id) == (CATEGORY_NATURE_FIXED, None)
    set_category_nature(db, comun.id, aporte.id, None)
    assert aporte.nature is None


def test_set_category_nature_refuses_bad_input(db, home):
    comun, demo = home["users"]["COMUN"], home["users"]["DEMO"]
    with pytest.raises(AdminError):                     # type nodes are neither
        set_category_nature(db, comun.id, home["gasto"].id, CATEGORY_NATURE_FIXED)
    with pytest.raises(AdminError):
        set_category_nature(db, comun.id, home["cats"]["luz"].id, "mensual")
    with pytest.raises(AdminError):                     # the demo account is not family
        set_category_nature(db, comun.id, home["cats"]["aporte"].id, CATEGORY_NATURE_TRANSFER, demo.id)
    with pytest.raises(AdminError):                     # nor is oneself
        set_category_nature(db, comun.id, home["cats"]["aporte"].id, CATEGORY_NATURE_TRANSFER, comun.id)
    with pytest.raises(NotFound):                       # another user's category
        set_category_nature(db, home["users"]["MARIA"].id, home["cats"]["luz"].id, CATEGORY_NATURE_FIXED)


def test_transfer_peers_never_include_demo_and_demo_sees_nobody(db, home):
    users = home["users"]
    assert [u.username for u in transfer_peers(db, users["COMUN"].id)] == ["MARIA", "PEDRO"]
    assert transfer_peers(db, users["DEMO"].id) == []


def test_movement_override_is_owned_and_validated(db, home):
    comun, maria = home["users"]["COMUN"], home["users"]["MARIA"]
    super_txn = home["txns"]["super"]
    set_transaction_fixed(db, comun.id, super_txn.id, FIXED_OVERRIDE_FIXED)
    assert super_txn.fixed_override == FIXED_OVERRIDE_FIXED
    set_transaction_fixed(db, comun.id, super_txn.id, None)
    assert super_txn.fixed_override is None
    with pytest.raises(NotFound):
        set_transaction_fixed(db, maria.id, super_txn.id, FIXED_OVERRIDE_FIXED)
    with pytest.raises(AdminError):
        set_transaction_fixed(db, comun.id, super_txn.id, "siempre")


def test_search_filter_and_rows_use_the_effective_nature(db, home):
    comun = home["users"]["COMUN"]
    cats, txns = home["cats"], home["txns"]
    set_natures(db, comun.id, {cats["luz"].id: CATEGORY_NATURE_FIXED,
                               cats["aporte"].id: CATEGORY_NATURE_TRANSFER})
    set_transaction_fixed(db, comun.id, txns["luz"].id, FIXED_OVERRIDE_VARIABLE)     # one-off: not a bill
    set_transaction_fixed(db, comun.id, txns["super"].id, FIXED_OVERRIDE_FIXED)
    uncategorized = Transaction(user_id=comun.id, account_id=txns["luz"].account_id,
                                transaction_date=date(2026, 9, 2), amount=-5,
                                type=TransactionType.expense, issuer_name="CAFE")
    db.add(uncategorized)
    db.commit()

    def ids(nature):
        rows, _ = search_transactions(db, comun.id, TransactionFilters(nature=nature))
        return {r.id for r in rows}

    assert ids(NATURE_FILTER_FIXED) == {txns["super"].id}
    assert ids(NATURE_FILTER_TRANSFER) == {txns["aporte"].id}
    assert ids(NATURE_FILTER_VARIABLE) == {txns["luz"].id, uncategorized.id}

    rows, _ = search_transactions(db, comun.id, TransactionFilters())
    by_id = {r.id: r for r in rows}
    assert by_id[txns["luz"].id].nature is None
    assert by_id[txns["luz"].id].category_nature == CATEGORY_NATURE_FIXED
    assert by_id[txns["aporte"].id].nature == CATEGORY_NATURE_TRANSFER


def test_suggestions_only_for_still_variable_categories(db, home):
    comun = home["users"]["COMUN"]
    cats = home["cats"]
    assert suggested_natures(db, comun.id) == {
        cats["luz"].id: CATEGORY_NATURE_FIXED, cats["aporte"].id: CATEGORY_NATURE_TRANSFER,
    }
    set_category_nature(db, comun.id, cats["luz"].id, CATEGORY_NATURE_FIXED)
    assert cats["luz"].id not in suggested_natures(db, comun.id)
    # Words inside other words do not count («Gasolina» is not «gas»… but starts with it,
    # so only word starts are matched: «Vegas» is not a bill).
    vegas = Category(user_id=comun.id, parent_id=home["gasto"].id, name="Viaje Vegas",
                     level=CategoryLevel.category)
    db.add(vegas)
    db.commit()
    assert vegas.id not in suggested_natures(db, comun.id)


def test_library_carries_natures_but_not_the_peer(db, home):
    comun, maria, pedro = (home["users"][n] for n in ("COMUN", "MARIA", "PEDRO"))
    cats = home["cats"]
    set_category_nature(db, comun.id, cats["luz"].id, CATEGORY_NATURE_FIXED)
    set_category_nature(db, comun.id, cats["aporte"].id, CATEGORY_NATURE_TRANSFER, maria.id)

    payload = library_io.export_library(db, comun.id)
    gasto = next(t for t in payload["types"] if t["name"] == "GASTO")
    assert gasto["natures"] == {"Luz": CATEGORY_NATURE_FIXED}
    lib = library_io.parse_library(library_io.dumps(payload))

    # PEDRO already has «Luz» marked variable-by-default: it gets the mark;
    # a category he marked himself keeps his choice.
    gasto_p = Category(user_id=pedro.id, name="GASTO", level=CategoryLevel.type,
                       transaction_type=TransactionType.expense)
    db.add(gasto_p)
    db.flush()
    luz_p = Category(user_id=pedro.id, parent_id=gasto_p.id, name="LUZ", level=CategoryLevel.category)
    super_p = Category(user_id=pedro.id, parent_id=gasto_p.id, name="Supermercados",
                       level=CategoryLevel.category, nature=CATEGORY_NATURE_FIXED)
    db.add_all([luz_p, super_p])
    db.commit()

    plan = library_io.plan_import(db, pedro.id, lib)
    assert "GASTO › Luz: fijo" in plan.nature_changes
    assert "INGRESO › Aporte Comun María: traspaso interno" in plan.nature_changes
    result = library_io.apply_import(db, pedro.id, lib)
    assert result.natures_set == 2
    assert luz_p.nature == CATEGORY_NATURE_FIXED
    assert super_p.nature == CATEGORY_NATURE_FIXED
    aporte_p = db.query(Category).filter_by(user_id=pedro.id, name="Aporte Comun María").one()
    assert (aporte_p.nature, aporte_p.transfer_peer_user_id) == (CATEGORY_NATURE_TRANSFER, None)


def test_library_rejects_a_nature_for_an_unknown_category():
    payload = {"format": library_io.FORMAT_NAME, "format_version": library_io.FORMAT_VERSION,
               "types": [{"name": "GASTO", "transaction_type": "expense", "categories": ["Luz"],
                          "natures": {"Agua": "fijo"}}], "rules": []}
    with pytest.raises(library_io.LibraryError):
        library_io.parse_library(library_io.dumps(payload))
