"""
Search and filters of the Reglas screen (app.categorization.rule_search),
the desktop RulesView on a filtered/sorted table (actions must hit the rule
of the selected row, not the row index of the unfiltered list) and the web
/mas/reglas page.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.categorization.rule_search import (
    SCOPE_GLOBAL,
    SCOPE_PERSONAL,
    USAGE_WITH,
    USAGE_WITHOUT,
    RuleFilters,
    build_rule_rows,
    count_label,
    filter_options,
    filter_rule_rows,
)
from app.db.models import (
    AutoCategorizationRule,
    Base,
    Category,
    CategoryLevel,
    MatchType,
    TransactionType,
    User,
)
# Web fixtures (pytest picks up fixtures imported into the module).
from tests.test_web import _hx, app, factory, family, logged_in  # noqa: F401


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


def _cat(s, user, name, parent=None, ttype=None):
    cat = Category(user_id=user.id, name=name,
                   parent_id=parent.id if parent else None,
                   level=CategoryLevel.category if parent else CategoryLevel.type,
                   transaction_type=ttype)
    s.add(cat)
    s.flush()
    return cat


@pytest.fixture
def rules(db_session):
    s = db_session
    ana = User(username="ana", password_hash="x")
    beto = User(username="beto", password_hash="x")
    s.add_all([ana, beto])
    s.flush()
    gasto = _cat(s, ana, "GASTO", ttype=TransactionType.expense)
    ingreso = _cat(s, ana, "INGRESO", ttype=TransactionType.income)
    alim = _cat(s, ana, "Alimentación", gasto)
    transp = _cat(s, ana, "Transporte", gasto)
    nomina = _cat(s, ana, "Nómina", ingreso)
    b_gasto = _cat(s, beto, "GASTO", ttype=TransactionType.expense)
    b_alim = _cat(s, beto, "Alimentación", b_gasto)
    b_ropa = _cat(s, beto, "Ropa", b_gasto)

    def rule(user, cat, pattern, mt, priority):
        r = AutoCategorizationRule(user_id=user.id if user else None, category_id=cat.id,
                                   match_pattern=pattern, match_type=mt, priority=priority,
                                   created_at=datetime(2026, 3, 7, 12, 0))
        s.add(r)
        s.flush()
        return r.id

    ids = {
        "mercadona": rule(ana, alim, "MERCADONA", MatchType.contains, 10),
        "repsol": rule(ana, transp, "^REPSOL", MatchType.regex, 20),
        "nomina": rule(ana, nomina, "NOMINA EMPRESA", MatchType.exact, 5),
        "lidl": rule(None, b_alim, "LIDL", MatchType.contains, 100),     # global → ana's Alimentación
        "zara": rule(None, b_ropa, "ZARA", MatchType.contains, 100),     # global, no equivalent for ana
        "secret": rule(beto, b_alim, "SECRETO", MatchType.contains, 1),  # beto's own: never shown to ana
    }
    s.commit()
    return s, ana, ids, {"alim": alim.id, "transp": transp.id}


def _shown(rows, **kw):
    return {r.id for r in filter_rule_rows(rows, RuleFilters(**kw))}


def test_rows_resolve_categories_and_scope(rules):
    s, ana, ids, cats = rules
    rows = {r.id: r for r in build_rule_rows(s, ana.id)}
    assert ids["secret"] not in rows and len(rows) == 5
    assert rows[ids["lidl"]].category_id == cats["alim"]          # mapped to ana's tree
    assert rows[ids["lidl"]].type_name == "GASTO" and rows[ids["lidl"]].is_global
    assert rows[ids["zara"]].category_id is None and rows[ids["zara"]].category_name == "Ropa"
    categories, types = filter_options(list(rows.values()))
    assert [c[1] for c in categories] == ["Alimentación", "Transporte", "Nómina"]
    assert types == ["GASTO", "INGRESO"]


def test_search_each_field(rules):
    s, ana, ids, _ = rules
    rows = build_rule_rows(s, ana.id, matches={ids["mercadona"]: 3})
    assert _shown(rows, text="mercadona") == {ids["mercadona"]}                 # pattern
    assert _shown(rows, text="repsol") == {ids["repsol"]}                       # pattern with regex chars
    assert _shown(rows, text="regex") == {ids["repsol"]}                        # match type label/value
    assert _shown(rows, text="expresión regular") == {ids["repsol"]}            # web wording
    assert _shown(rows, text="exacto") == {ids["nomina"]}
    assert _shown(rows, text="alimentacion") == {ids["mercadona"], ids["lidl"]}  # category, accent-folded
    assert _shown(rows, text="ropa") == {ids["zara"]}                           # unresolved global's category
    assert _shown(rows, text="ingreso") == {ids["nomina"]}                      # parent type
    assert _shown(rows, text="global") == {ids["lidl"], ids["zara"]}            # scope
    assert _shown(rows, text="personal") == {ids["mercadona"], ids["repsol"], ids["nomina"]}
    assert _shown(rows, text="20") == {ids["repsol"]}                           # priority, whole number
    assert _shown(rows, text="3") == {ids["mercadona"]}                         # linked transactions
    assert _shown(rows, text="07/03/2026") == set(ids.values()) - {ids["secret"]}  # creation date
    assert _shown(rows, text="2026") == set()                                   # a bare year is not a date


def test_search_and_words_case_and_accents(rules):
    s, ana, ids, _ = rules
    rows = build_rule_rows(s, ana.id)
    assert _shown(rows, text="mercadona alimentación") == {ids["mercadona"]}
    assert _shown(rows, text="mercadona transporte") == set()
    assert _shown(rows, text="GLOBAL Alimentacion") == {ids["lidl"]}
    assert _shown(rows, text="NÓMINA") == {ids["nomina"]}
    assert _shown(rows, text="nomina   EXACTO   ingreso") == {ids["nomina"]}
    assert _shown(rows, text="   ") == {r.id for r in rows}


def test_filters_combine_with_search(rules):
    s, ana, ids, cats = rules
    rows = build_rule_rows(s, ana.id, matches={ids["mercadona"]: 3, ids["lidl"]: 1})
    assert _shown(rows, category_id=cats["alim"]) == {ids["mercadona"], ids["lidl"]}
    assert _shown(rows, type_name="gasto") == {ids["mercadona"], ids["repsol"], ids["lidl"], ids["zara"]}
    assert _shown(rows, match_type=MatchType.regex) == {ids["repsol"]}
    assert _shown(rows, scope=SCOPE_GLOBAL) == {ids["lidl"], ids["zara"]}
    assert _shown(rows, scope=SCOPE_PERSONAL, usage=USAGE_WITHOUT) == {ids["repsol"], ids["nomina"]}
    assert _shown(rows, usage=USAGE_WITH) == {ids["mercadona"], ids["lidl"]}
    assert _shown(rows, category_id=cats["alim"], text="global") == {ids["lidl"]}
    assert _shown(rows, scope=SCOPE_GLOBAL, text="mercadona") == set()
    assert RuleFilters().is_empty() and not RuleFilters(scope=SCOPE_GLOBAL).is_empty()


def test_count_label():
    assert count_label(37, 412, True) == "Mostrando 37 de 412 reglas"
    assert count_label(412, 412, False) == "412 reglas"
    assert count_label(1, 1, False) == "1 regla"


def test_thousand_rules_build_and_filter_fast(rules):
    s, ana, ids, cats = rules
    s.add_all(
        AutoCategorizationRule(user_id=ana.id, category_id=cats["alim" if i % 2 else "transp"],
                               match_pattern=f"COMERCIO NÚMERO {i}", match_type=MatchType.contains,
                               priority=1 + i % 900)
        for i in range(1000)
    )
    s.commit()
    start = time.perf_counter()
    rows = build_rule_rows(s, ana.id, matches={})
    built = time.perf_counter() - start
    start = time.perf_counter()
    for text in ("comercio", "numero 99 transporte", "alimentacion personal", "zzz"):
        filter_rule_rows(rows, RuleFilters(text=text, usage=USAGE_WITHOUT))
    filtered = (time.perf_counter() - start) / 4
    assert len(rows) == 1005
    assert len(_shown(rows, text="número 999")) == 1
    assert built < 2.0 and filtered < 0.1, (built, filtered)


# ---------------------------------------------------------------------------
# Desktop RulesView
# ---------------------------------------------------------------------------

@pytest.fixture
def rules_view(rules, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from app.gui.rules_view import RulesView
    s, ana, ids, cats = rules
    view = RulesView(ana, s, "light")
    yield view, s, ids, cats
    view.deleteLater()


def _select_row(view, row):
    view._table.setCurrentIndex(view._model.index(row, 1))


def _shown_patterns(view):
    return [view._model.item(r, 1).text() for r in range(view._model.rowCount())]


def test_rules_view_search_filters_and_count(rules_view):
    view, _s, ids, cats = rules_view
    assert view._inp_search.placeholderText() == "Buscar reglas…"
    assert view._lbl_count.text() == "5 reglas" and not view._btn_clear.isEnabled()
    assert _shown_patterns(view) == ["NOMINA EMPRESA", "MERCADONA", "^REPSOL", "LIDL", "ZARA"]

    view._inp_search.setText("alimentacion")
    assert view._search_timer.isActive()            # debounced
    view._on_filter_changed()                       # what the timer fires
    assert set(_shown_patterns(view)) == {"MERCADONA", "LIDL"}
    assert view._lbl_count.text() == "Mostrando 2 de 5 reglas" and view._btn_clear.isEnabled()

    view._cmb_scope.setCurrentIndex(view._cmb_scope.findData(SCOPE_GLOBAL))
    assert _shown_patterns(view) == ["LIDL"]
    view._clear_filters()
    assert view._lbl_count.text() == "5 reglas" and view._cmb_scope.currentIndex() == 0
    assert view._inp_search.text() == ""

    view._cmb_cat.setCurrentIndex(view._cmb_cat.findData(cats["transp"]))
    assert _shown_patterns(view) == ["^REPSOL"]
    view._cmb_cat.setCurrentIndex(0)
    view._cmb_match.setCurrentIndex(view._cmb_match.findData(MatchType.exact))
    assert _shown_patterns(view) == ["NOMINA EMPRESA"]

    for theme in ("dark", "light"):
        view.apply_theme(theme)
        view.grab()


def test_rules_view_actions_hit_the_selected_rule_when_filtered_and_sorted(rules_view, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QMessageBox
    import app.gui.rules_view as module

    view, s, ids, _ = rules_view
    # Sorting by priority puts NOMINA (5) first; the search leaves only
    # rules whose position differs from the unfiltered list.
    view._inp_search.setText("alimentacion")
    view._on_filter_changed()
    view._table.sortByColumn(1, Qt.SortOrder.DescendingOrder)   # by pattern, Z→A
    assert _shown_patterns(view) == ["MERCADONA", "LIDL"]

    _select_row(view, 1)
    assert view._selected_rule().id == ids["lidl"]

    # Edit: the dialog is accepted unchanged; update_rule must get LIDL's id.
    edited = []
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(module, "update_rule", lambda session, rule_id, **kw: edited.append(rule_id))
    view._on_edit_rule()
    assert edited == [ids["lidl"]]

    # Delete the first shown row (MERCADONA, row 1 of the unfiltered list).
    view._inp_search.setText("alimentacion")
    view._on_filter_changed()
    _select_row(view, 0)
    assert view._selected_rule().id == ids["mercadona"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view._on_delete()
    assert s.get(AutoCategorizationRule, ids["mercadona"]) is None
    for key in ("repsol", "nomina", "lidl", "zara"):
        assert s.get(AutoCategorizationRule, ids[key]) is not None
    # The filter and the sort survive the refresh after the change.
    assert view._inp_search.text() == "alimentacion"
    assert _shown_patterns(view) == ["LIDL"]
    assert view._lbl_count.text() == "Mostrando 1 de 4 reglas"


# ---------------------------------------------------------------------------
# Web /mas/reglas
# ---------------------------------------------------------------------------

def test_web_rules_search_and_filters(app, family, factory):  # noqa: F811
    db = factory()
    try:
        db.add_all([
            AutoCategorizationRule(user_id=family["ana"], category_id=family["ana_cat"],
                                   match_pattern="MERCADO", match_type=MatchType.contains, priority=50),
            AutoCategorizationRule(user_id=family["ana"], category_id=family["ana_cat"],
                                   match_pattern="CARREFOUR", match_type=MatchType.exact, priority=60),
            AutoCategorizationRule(user_id=None, category_id=family["beto_cat"],
                                   match_pattern="ALCAMPO", match_type=MatchType.regex, priority=70),
        ])
        db.commit()
    finally:
        db.close()
    client = logged_in(app)

    page = client.get("/mas/reglas").text
    assert "Buscar reglas…" in page and "3 reglas" in page
    assert all(p in page for p in ("«MERCADO»", "«CARREFOUR»", "«ALCAMPO»"))

    page = client.get("/mas/reglas", params={"q": "supermercados EXACTAMENTE"}).text
    assert "«CARREFOUR»" in page and "«MERCADO»" not in page and "«ALCAMPO»" not in page
    assert "Mostrando 1 de 3 reglas" in page and "Quitar filtros" in page

    page = client.get("/mas/reglas", params={"ambito": "global"}).text
    assert "«ALCAMPO»" in page and "«CARREFOUR»" not in page

    page = client.get("/mas/reglas", params={"uso": "con"}).text      # MERCADO wins "MERCADO ANA"
    assert "«MERCADO»" in page and "«CARREFOUR»" not in page

    page = client.get("/mas/reglas", params={"coincidencia": "regex", "q": "alcampo"}).text
    assert "Mostrando 1 de 3 reglas" in page and "«ALCAMPO»" in page

    page = client.get("/mas/reglas", params={"q": "nada-que-coincida"}).text
    assert "Sin resultados" in page and "Mostrando 0 de 3 reglas" in page

    # HTMX request for the results only still renders the whole page; the
    # client swaps #rules-results out of it (hx-select).
    response = client.get("/mas/reglas", params={"q": "mercado"}, headers=_hx(client))
    assert response.status_code == 200 and 'id="rules-results"' in response.text
