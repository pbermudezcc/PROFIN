"""
tests/test_account_dialogs.py
=============================
PC «Cambiar contraseña» / «Cambiar nombre» (account menu): the shared
account_service rules, errors under the right field, and what MainWindow
does afterwards (web sessions, audit, labels). Offscreen, throwaway DB.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.auth_service import create_user, password_matches
from app.config import THEMES
from app.db.models import AuditLog, Base, User

PASSWORD = "clave-secreta-1"
NEW_PASSWORD = "otra frase larga 7"


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


@pytest.fixture
def ana(session):
    return create_user(session, "ana", PASSWORD)


def _fill(dialog, **values):
    for key, value in values.items():
        dialog.inputs[key].setText(value)


def test_wrong_current_password_is_shown_under_its_field(qt_app, session, ana):
    from app.gui.account_dialogs import ChangePasswordDialog

    dialog = ChangePasswordDialog(session, ana.id, THEMES["light"])
    _fill(dialog, current="no-es-esta", password=NEW_PASSWORD, confirm=NEW_PASSWORD)
    dialog.submit()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.errors["current"].isVisibleTo(dialog)
    assert "no es correcta" in dialog.errors["current"].text()
    assert dialog.inputs["current"].text() == ""
    assert password_matches(session.get(User, ana.id), PASSWORD)


def test_new_password_rules_and_success(qt_app, session, ana):
    from app.gui.account_dialogs import ChangePasswordDialog

    dialog = ChangePasswordDialog(session, ana.id, THEMES["dark"])
    _fill(dialog, current=PASSWORD, password="corta", confirm="corta")
    dialog.submit()
    assert dialog.errors["password"].isVisibleTo(dialog)

    _fill(dialog, current=PASSWORD, password=NEW_PASSWORD, confirm=NEW_PASSWORD)
    dialog.submit()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert password_matches(session.get(User, ana.id), NEW_PASSWORD)


def test_rename_refuses_a_taken_name_then_renames(qt_app, session, ana):
    from app.gui.account_dialogs import RenameDialog

    create_user(session, "beto", "clave-secreta-2")
    dialog = RenameDialog(session, ana.id, THEMES["light"])
    _fill(dialog, username="Beto", password=PASSWORD)
    dialog.submit()
    assert dialog.errors["username"].isVisibleTo(dialog)

    _fill(dialog, username="Ana María", password=PASSWORD)
    dialog.submit()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.names == ("ana", "Ana María")


@pytest.fixture
def window(qt_app, session, ana, monkeypatch):
    from app.gui import main_window as main_window_module

    monkeypatch.setattr(main_window_module.MainWindow, "_run_auto_backup", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    win = main_window_module.MainWindow(user=ana, session=session, theme="light")
    yield win
    win.close()
    win.deleteLater()


def _auto_accept(monkeypatch, cls, **values):
    def fake_exec(dialog):
        _fill(dialog, **values)
        dialog.submit()
        return dialog.result()
    monkeypatch.setattr(cls, "exec", fake_exec)


def test_pc_password_change_ends_the_web_sessions(window, session, ana, monkeypatch):
    from app.gui.account_dialogs import ChangePasswordDialog
    from app.web.server import get_web_server

    sessions = get_web_server().sessions
    sessions.create(ana.id, "ana", "192.168.1.30", "iPad")
    sessions.create(ana.id, "ana", "192.168.1.31", "iPhone")
    _auto_accept(monkeypatch, ChangePasswordDialog,
                 current=PASSWORD, password=NEW_PASSWORD, confirm=NEW_PASSWORD)

    window._on_change_password()

    assert not [s for s in sessions.active() if s.user_id == ana.id]
    row = session.query(AuditLog).filter_by(table_affected="users").one()
    assert "contraseña cambiada; 2 sesiones web cerradas [PC]" == row.new_value


def test_pc_rename_updates_labels_web_sessions_and_audit(window, session, ana, monkeypatch):
    from app.gui.account_dialogs import RenameDialog
    from app.web.server import get_web_server

    sessions = get_web_server().sessions
    web = sessions.create(ana.id, "ana", "192.168.1.30", "iPad")
    _auto_accept(monkeypatch, RenameDialog, username="Anita", password=PASSWORD)

    window._on_rename_account()

    assert window._lbl_sidebar_name.text() == "Anita"
    assert sessions.get(web.sid).username == "Anita"
    row = session.query(AuditLog).filter_by(table_affected="users").one()
    assert (row.old_value, row.new_value) == ("«ana»", "«Anita» [PC]")
    sessions.revoke_user(ana.id)


def test_pc_export_saves_only_my_data(window, session, ana, tmp_path, monkeypatch):
    import zipfile
    from PySide6.QtWidgets import QFileDialog

    target = tmp_path / "mis-datos.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))

    window._on_export_my_data()

    names = zipfile.ZipFile(target).namelist()
    assert "LEEME.txt" in names and any(n.endswith(".xlsx") for n in names)
    row = session.query(AuditLog).filter_by(table_affected="users").one()
    assert row.new_value.startswith("exportó sus datos: 0 movimientos")
