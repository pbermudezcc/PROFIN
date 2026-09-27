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

    user = create_user(session, "ana", "secreto1")
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

        for key in ("expenses", "income", "transactions", "rules", "categories"):
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
    finally:
        window.close()
        window.deleteLater()
