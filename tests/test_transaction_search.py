"""
Free-text search of Movimientos/Transacciones over all transaction fields
(app.analytics.transaction_search): AND across words, OR across fields,
case- and accent-insensitive, combined with the structured filters.
"""
from __future__ import annotations

import time
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analytics.transaction_search import (
    CONCEPT_COLUMNS,
    TransactionFilters,
    filtered_transactions_page,
    fold_text,
    matching_transaction_ids,
    search_terms,
    search_transactions,
)
from app.db.models import (
    CATEGORY_SOURCE_MANUAL,
    Account,
    Base,
    Category,
    CategoryLevel,
    Transaction,
    TransactionType,
    User,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


def _txn(user, account, **kw):
    amount = kw.pop("amount", -10.0)
    defaults = dict(
        user_id=user.id, account_id=account.id,
        transaction_date=date(2026, 3, 15), amount=amount,
        type=TransactionType.expense if amount < 0 else TransactionType.income,
    )
    defaults.update(kw)
    return Transaction(**defaults)


@pytest.fixture
def data(db_session):
    s = db_session
    user = User(username="ana", password_hash="x")
    other = User(username="otro", password_hash="x")
    s.add_all([user, other])
    s.flush()
    acc = Account(user_id=user.id, account_name="Cuenta Nómina", bank="CaixaBank", iban_last4="4321")
    acc2 = Account(user_id=user.id, account_name="Ahorro", bank="CaixaBank", iban_last4="9999")
    other_acc = Account(user_id=other.id, account_name="Cuenta Nómina", bank="CaixaBank")
    s.add_all([acc, acc2, other_acc])
    s.flush()
    gasto = Category(user_id=user.id, name="GASTO", level=CategoryLevel.type,
                     transaction_type=TransactionType.expense)
    s.add(gasto)
    s.flush()
    alim = Category(user_id=user.id, parent_id=gasto.id, name="Alimentación",
                    level=CategoryLevel.category)
    s.add(alim)
    s.flush()
    t = {
        "super": _txn(user, acc, issuer_name="MERCADONA", concept="COMPRA TARJ",
                      amount=-45.0, category_id=alim.id, category_source=CATEGORY_SOURCE_MANUAL,
                      running_balance=1234.56),
        "luz": _txn(user, acc, issuer_name="IBERDROLA", concept="RECIBO LUZ", amount=-12.5,
                    transaction_date=date(2026, 2, 3), concepto_comun="06",
                    concepto_propio="123", running_balance=900.0),
        "nomina": _txn(user, acc2, issuer_name="EMPRESA SL", concept="NOMINA MARZO",
                       amount=1500.0, running_balance=3000.0, source_file="extracto_marzo.xls"),
        "raw": _txn(user, acc2, issuer_name="TRANSF", concept="", amount=-1.0,
                    raw_col14="15/03 BIZUM PEPE", raw_details="Cena cumpleaños  Pepe",
                    running_balance=10.0),
        "other": _txn(other, other_acc, issuer_name="MERCADONA", amount=-45.0),
    }
    s.add_all(t.values())
    s.commit()
    return s, user, {k: v.id for k, v in t.items()}


def _ids(session, user, text, **filters):
    rows, total = search_transactions(session, user.id, TransactionFilters(text=text, **filters), 0, 100)
    assert total == len(rows)
    return {r.id for r in rows}


def test_fold_and_terms():
    assert fold_text("Alimentación ÑU") == "alimentacion nu"
    assert search_terms("  Luz  luz  Recibo ") == ["luz", "recibo"]
    assert search_terms("   ") == []


def test_every_concept_column_is_searched(data):
    session, user, ids = data
    names = {c.key for c in CONCEPT_COLUMNS}
    assert names == {"issuer_name", "concept", "concepto_comun", "concepto_propio",
                     "raw_col14", "raw_details"}
    assert _ids(session, user, "mercadona") == {ids["super"]}      # issuer_name
    assert _ids(session, user, "recibo") == {ids["luz"]}           # concept
    assert _ids(session, user, "06") >= {ids["luz"]}               # concepto_comun
    assert ids["luz"] in _ids(session, user, "123")                # concepto_propio
    assert _ids(session, user, "bizum") == {ids["raw"]}            # raw_col14 only
    assert _ids(session, user, "cumpleanos") == {ids["raw"]}       # raw_details, accent-folded


def test_non_concept_fields(data):
    session, user, ids = data
    assert _ids(session, user, "alimentacion") == {ids["super"]}   # category
    assert _ids(session, user, "GASTO alimentación") == {ids["super"]}  # parent type + category
    assert _ids(session, user, "ahorro") == {ids["nomina"], ids["raw"]}  # account name
    assert _ids(session, user, "4321") == {ids["super"], ids["luz"]}     # IBAN last 4
    assert _ids(session, user, "extracto_marzo") == {ids["nomina"]}      # source file
    assert _ids(session, user, "sin categorizar") == {ids["luz"], ids["nomina"], ids["raw"]}
    assert _ids(session, user, "manual") == {ids["super"]}               # category source
    assert ids["nomina"] in _ids(session, user, "ingreso")               # type label
    assert _ids(session, user, "03/02/2026") == {ids["luz"]}             # date dd/mm/yyyy
    assert _ids(session, user, "2026-02-03") == {ids["luz"]}             # date ISO


def test_amount_formats(data):
    session, user, ids = data
    assert _ids(session, user, "12,50") == {ids["luz"]}
    assert _ids(session, user, "12.50") == {ids["luz"]}
    assert _ids(session, user, "-45") == {ids["super"]}
    assert _ids(session, user, "1.500,00") == {ids["nomina"]}
    assert _ids(session, user, "+1500") == {ids["nomina"]}
    assert _ids(session, user, "1.234,56") == {ids["super"]}             # running balance


def test_multiple_words_are_anded(data):
    session, user, ids = data
    assert _ids(session, user, "mercadona alimentacion") == {ids["super"]}
    assert _ids(session, user, "mercadona luz") == set()
    assert _ids(session, user, "nomina ahorro") == {ids["nomina"]}
    # Matching words may come from different fields; case is ignored.
    assert _ids(session, user, "IberDrola 12,50 Nómina") == {ids["luz"]}


def test_search_respects_filters_and_user_scope(data):
    session, user, ids = data
    assert _ids(session, user, "mercadona") == {ids["super"]}            # other user's row excluded
    assert _ids(session, user, "cuenta", txn_type=TransactionType.income) == set()
    assert _ids(session, user, "caixabank", txn_type=TransactionType.income) == {ids["nomina"]}
    assert _ids(session, user, "caixabank", date_from=date(2026, 3, 1)) == {
        ids["super"], ids["nomina"], ids["raw"]}
    assert _ids(session, user, "caixabank", category_id=-1, min_amount=10) == {
        ids["luz"], ids["nomina"]}


def test_pagination_with_search(data):
    session, user, ids = data
    query = session.query(Transaction).filter(Transaction.user_id == user.id)
    page, total = filtered_transactions_page(session, user.id, query, "caixabank", 1, 2)
    ordered = matching_transaction_ids(session, user.id, query, "caixabank")
    assert total == 4 and [t.id for t in page] == ordered[1:3]
    page, total = filtered_transactions_page(session, user.id, query, "", 0, 2)
    assert total == 4 and len(page) == 2


def test_search_stays_fast_on_thousands_of_rows(data):
    session, user, _ = data
    acc = session.query(Account).filter_by(user_id=user.id).first()
    session.add_all(
        _txn(user, acc, issuer_name=f"COMERCIO {i}", concept="COMPRA TARJETA CONTACTLESS",
             raw_details=f"detalle {i}", amount=-(i % 300) - 0.25, running_balance=1000.0 + i,
             transaction_date=date(2025, 1 + i % 12, 1 + i % 28))
        for i in range(5000)
    )
    session.commit()
    query = session.query(Transaction).filter(Transaction.user_id == user.id)
    start = time.perf_counter()
    page, total = filtered_transactions_page(session, user.id, query, "comercio 12,25", 0, 100)
    elapsed = time.perf_counter() - start
    assert total > 0 and all("12,25" in f"{abs(t.amount):.2f}".replace(".", ",") or
                             "12,25" in f"{t.running_balance:.2f}".replace(".", ",")
                             for t in page)
    assert elapsed < 1.5, elapsed


def test_desktop_transactions_view_searches_all_fields(data, monkeypatch):
    """TransactionsView (offscreen) uses the shared search, debounced."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from app.gui.transactions_view import TransactionsView

    session, user, ids = data
    for theme in ("light", "dark"):
        view = TransactionsView(user, session, theme)
        assert "todos los campos" in view._inp_search.placeholderText()
        view._inp_search.setText("Alimentacion  -45")
        assert view._search_timer.isActive()        # debounced, not yet applied
        view._on_filter_changed()                   # what the timer fires
        assert view._total == 1 and view._txn_ids == [ids["super"]]
        view._inp_search.setText("cumpleaños")
        view._on_filter_changed()
        assert view._txn_ids == [ids["raw"]]
        view._cmb_type.setCurrentIndex(1)           # Ingresos + search → nothing
        assert view._total == 0
        view._clear_filters()
        assert view._total == 4 and not view._search_timer.isActive()
        view.grab()
        view.deleteLater()
    app.processEvents()
