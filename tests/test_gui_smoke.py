"""
tests/test_gui_smoke.py
========================
Offscreen smoke test of the main shell: views are built lazily on first
navigation, the data views load from a throwaway database, and a theme
toggle reaches every built view. No real data folder is touched.
"""
from __future__ import annotations

import os
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.auth_service import create_user
from app.db.models import Account, Base, Transaction, TransactionType


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'profin.db'}")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


def test_main_window_builds_views_lazily_and_themes_them(qt_app, session, monkeypatch, caplog):
    from app.gui import main_window as main_window_module

    # Automatic backups would snapshot the real data folder.
    monkeypatch.setattr(main_window_module.MainWindow, "_run_auto_backup", lambda self: None)

    user = create_user(session, "ana", "clave-secreta-1")
    account = Account(user_id=user.id, account_name="Cuenta")
    session.add(account)
    session.flush()
    today = date.today()
    for amount in (-12.5, -7.25, 1500.0):
        session.add(Transaction(
            user_id=user.id, account_id=account.id, transaction_date=today,
            amount=amount,
            type=TransactionType.expense if amount < 0 else TransactionType.income,
            issuer_name="PRUEBA",
        ))
    session.commit()

    window = main_window_module.MainWindow(user=user, session=session, theme="light")
    window.show()  # as LoginWindow does after constructing it
    # Children added to an already visible window are shown by a queued call
    # on the next event-loop pass; the real app runs the loop, the test must.
    qt_app.processEvents()
    try:
        window._navigate("dashboard")
        assert set(window._views) == {"dashboard"}

        for key in ("expenses", "income", "investments", "transactions", "rules", "categories", "forecast"):
            window._navigate(key)
            assert window._stack.currentWidget() is window._views[key]

        assert "import" not in window._views  # never visited → never built

        window._toggle_theme()
        for view in window._views.values():
            if hasattr(view, "_theme"):
                assert view._theme == window._theme

        window._navigate("unknown-view")
        assert window._current_view == "dashboard"

        # Views swallow load errors into the log; none may have happened.
        errors = [r.getMessage() for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
        assert errors == []
        assert "2 transacciones" in window._views["expenses"]._lbl_count.text()
        assert "1 transacciones" in window._views["income"]._lbl_count.text()

        # The help button opens the bundled manual in the browser.
        opened = []
        monkeypatch.setattr(main_window_module.QDesktopServices, "openUrl", opened.append)
        window._btn_help.click()
        assert opened and opened[0].toLocalFile().endswith("docs/manual/index.html")
    finally:
        window.close()
        window.deleteLater()


def test_library_import_dialog_previews_merge_and_replace(qt_app, session):
    from app.categorization import library_io
    from app.categorization.category_manager import seed_default_categories, seed_default_rules
    from app.gui.library_dialog import LibraryImportDialog

    ana = create_user(session, "ana", "clave-secreta-1")
    beto = create_user(session, "beto", "clave-secreta-2")
    seed_default_categories(session, ana.id)
    seed_default_rules(session, ana.id)
    lib = library_io.parse_library(library_io.dumps(library_io.export_library(session, ana.id)))

    dialog = LibraryImportDialog(session, beto.id, lib, "ana.profin-cats.json")
    assert dialog.mode == library_io.MODE_MERGE and dialog.plan.has_changes
    assert dialog._ok.isEnabled()
    dialog._replace.setChecked(True)
    assert dialog.mode == library_io.MODE_REPLACE and dialog.plan.mode == library_io.MODE_REPLACE

    # Already up to date: nothing to import, the button is disabled.
    same = LibraryImportDialog(session, ana.id, lib, "ana.profin-cats.json")
    assert not same.plan.has_changes and not same._ok.isEnabled()


def test_forecast_view_shows_the_household(qt_app):
    """Previsiones on the test household of test_forecast: Mío for the joint
    account (aportes + required contribution) and Familia."""
    from unittest import mock

    import tests.test_forecast as household
    from app.gui.forecast_view import ForecastView
    from tests.test_forecast import TODAY

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        home = household.home.__wrapped__(sess)
        real = household.user_forecast, household.family_forecast
        with mock.patch("app.gui.forecast_view.user_forecast",
                        lambda s, uid: real[0](s, uid, TODAY)), \
             mock.patch("app.gui.forecast_view.family_forecast", lambda s: real[1](s, TODAY)):
            view = ForecastView(home.users["COMUN"], sess, "light")
            view.show()
            qt_app.processEvents()
            assert view._cards["needed"].isVisible()
            assert view._tbl_aport.rowCount() == 2
            assert view._tbl_this.rowCount() == 3
            view._cmb_view.setCurrentIndex(1)                # Familia
            qt_app.processEvents()
            assert "COMUN, MARIA, PEDRO" in view._lbl_window.text()
            assert not view._cards["needed"].isVisible()
            assert "PEDRO" in view._lbl_warnings.text()
            view.apply_theme("dark")
            view.close()
    finally:
        sess.close()
        engine.dispose()
