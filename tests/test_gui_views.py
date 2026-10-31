"""
tests/test_gui_views.py
=======================
Offscreen tests of the desktop screens the rest of the suite barely reached:
the login window (sign in, wrong password, create an account), the import
wizard (statement → preview → import), Configuración (save, backup, restore
wiring, data-folder detection) and Categorías (add / rename / delete / Tipo
de gasto / defaults / export).

Every dialog is answered by monkeypatching its static Qt call, and every
database is a throwaway one: the app's data folder points at tmp_path for
the login tests and is put back afterwards (conftest guards the real data).
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit, QMessageBox
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.auth_service import create_user
from app.db import database
from app.db.models import (
    CATEGORY_NATURE_FIXED,
    CATEGORY_NATURE_TRANSFER,
    Account,
    AutoCategorizationRule,
    Base,
    Category,
    CategoryLevel,
    ImportBatch,
    Transaction,
    TransactionType,
)
from tests.test_import_pipeline import EXPORT_ROWS, _write_export


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'views.db'}")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def dialogs(monkeypatch):
    """Answer every QMessageBox: question → Yes; the others are recorded."""
    shown = []

    def record(kind):
        def _show(parent, title, text, *args, **kwargs):
            shown.append((kind, title, text))
            return QMessageBox.StandardButton.Ok
        return _show

    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(record(kind)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **k: shown.append(("question", a[1] if len(a) > 1 else "", "")) or QMessageBox.StandardButton.Yes))
    return shown


# ---------------------------------------------------------------------------
# Login window
# ---------------------------------------------------------------------------

@pytest.fixture
def app_data(tmp_path):
    """The app's data folder → tmp_path/datos with a fresh database; restored after."""
    folder = tmp_path / "datos"
    settings = QSettings("FAMILYFIN", "FAMILYFIN")
    settings.setValue("settings/data_folder", str(folder))
    settings.sync()
    database.init_db()
    yield folder
    from conftest import point_app_at_test_data_dir
    point_app_at_test_data_dir()
    database.refresh_runtime_paths()


def test_login_window_signs_in_and_rejects_a_wrong_password(qt_app, app_data, monkeypatch):
    from app.gui import login_window as login_module
    from app.gui import main_window as main_module

    with database.get_session() as s:
        create_user(s, "lucia", "clave-larga-lucia")
    opened = []

    class FakeMain:
        def __init__(self, user, session, theme):
            opened.append((user.username, theme))

        def show(self):
            pass

    monkeypatch.setattr(main_module, "MainWindow", FakeMain)
    window = login_module.LoginWindow()
    try:
        assert window._user_combo.findText("lucia") >= 0
        assert "BD encontrada" in window._database_status.text()

        window._user_combo.setCurrentText("lucia")
        window._on_login()                                    # no password
        assert "contraseña" in window._lbl_error.text()
        window._inp_pass.setText("mala")
        window._on_login()
        assert window._lbl_error.text() == "Usuario o contraseña incorrectos."
        assert window._inp_pass.text() == ""                  # cleared for another try
        assert opened == []

        window._inp_pass.setText("clave-larga-lucia")
        window._on_login()
        assert opened == [("lucia", window._theme)]
        assert QSettings("FAMILYFIN", "FAMILYFIN").value("auth/last_username") == "lucia"
    finally:
        window.close()
        window._session.close()


def test_login_window_creates_an_account(qt_app, app_data, monkeypatch):
    from app.gui import login_window as login_module

    answers = iter([("ab", "x", "x"),                                   # too short
                    ("pablo", "clave-larga-pablo", "otra-cosa"),        # mismatch
                    ("pablo", "clave-larga-pablo", "clave-larga-pablo")])
    errors = []

    def fake_exec(dlg):
        # Fill the three fields and press «Crear cuenta» until it closes.
        box = dlg.findChild(QDialogButtonBox)
        for user, pw, pw2 in answers:
            fields = dlg.findChildren(QLineEdit)
            for field, value in zip(fields, (user, pw, pw2)):
                field.setText(value)
            box.buttons()[0].click()
            if dlg.result() == QDialog.DialogCode.Accepted:
                return 1
            errors.append(next(lbl.text() for lbl in dlg.findChildren(login_module.QLabel)
                               if lbl.objectName() == "loginError"))
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window = login_module.LoginWindow()
    try:
        window._on_register()
        assert len(errors) == 2 and all(errors)               # both refused with a message
        assert window._user_combo.currentText() == "pablo"
        assert "creada" in window._lbl_error.text()
        assert database.get_database_users(app_data) == ["pablo"]
    finally:
        window.close()
        window._session.close()


def test_login_window_follows_family_changes_from_the_web(qt_app, app_data):
    from app.gui import login_window as login_module

    window = login_module.LoginWindow()
    try:
        assert window._user_combo.count() == 0
        with database.get_session() as s:
            create_user(s, "nuevo", "clave-larga-nuevo")
        window._on_web_users_changed(0, "transactions")        # not a user change: ignored
        assert window._user_combo.count() == 0
        window._on_web_users_changed(0, "user_created")
        assert window._user_combo.findText("nuevo") >= 0
        window._toggle_theme()
        window._toggle_theme()
    finally:
        window.close()
        window._session.close()


# ---------------------------------------------------------------------------
# Import wizard
# ---------------------------------------------------------------------------

def _worker_uses(session, monkeypatch):
    """The parse worker opens its own session (it runs in a thread): bind it
    to the test database instead of the app's."""
    monkeypatch.setattr(database, "get_session", lambda: sessionmaker(bind=session.get_bind())())


def test_import_view_previews_and_imports_a_statement(qt_app, session, tmp_path, dialogs, monkeypatch):
    from app.gui.import_view import ImportView, _ParseWorker

    _worker_uses(session, monkeypatch)

    user = create_user(session, "maria", "clave-secreta-1")
    path = tmp_path / "extracto.xlsx"
    _write_export(path, EXPORT_ROWS)

    view = ImportView(user, session, "light")
    try:
        assert view._account_id is not None                    # default account created
        view._select([path])
        assert view._btn_parse.isEnabled() and "extracto.xlsx" in view._lbl_selected.text()

        # The worker normally runs in a QThread; here it runs inline.
        worker = _ParseWorker([path], user.id, view._account_id)
        worker.finished.connect(view._on_parse_done)
        worker.error.connect(view._on_parse_error)
        worker.run()
        assert view._stack.currentIndex() == view.STEP_PREVIEW
        assert view._tbl_preview.rowCount() >= 4
        assert sum(view._decisions.values()) >= 4

        view._on_commit()
        assert view._stack.currentIndex() == view.STEP_SUMMARY
        assert "completada" in view._lbl_summary_title.text()
        assert session.query(Transaction).filter_by(user_id=user.id).count() >= 4
        assert session.query(ImportBatch).filter_by(user_id=user.id).count() == 1

        # The same statement again: everything is a duplicate, nothing new.
        before = session.query(Transaction).count()
        worker = _ParseWorker([path], user.id, view._account_id)
        worker.finished.connect(view._on_parse_done)
        worker.run()
        if sum(view._decisions.values()):
            view._on_commit()
        assert session.query(Transaction).count() == before
    finally:
        view.close()


def test_import_view_reports_an_unreadable_file(qt_app, session, tmp_path, dialogs, monkeypatch):
    from app.gui.import_view import ImportView, _ParseWorker

    _worker_uses(session, monkeypatch)

    user = create_user(session, "maria", "clave-secreta-1")
    bad = tmp_path / "no-es-excel.xlsx"
    bad.write_bytes(b"esto no es un excel")
    view = ImportView(user, session, "light")
    try:
        worker = _ParseWorker([bad], user.id, view._account_id)
        worker.finished.connect(view._on_parse_done)
        worker.error.connect(view._on_parse_error)
        worker.run()
        assert view._stack.currentIndex() in (view.STEP_SELECT, view.STEP_PREVIEW)
        assert dialogs, "the user must be told why the file was not imported"
        assert session.query(Transaction).count() == 0
    finally:
        view.close()


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def test_settings_view_saves_backs_up_and_wires_restore(qt_app, session, tmp_path, dialogs, monkeypatch):
    from app.gui import settings_view as settings_module
    from app.settings import SettingsManager

    user = create_user(session, "maria", "clave-secreta-1")
    database.refresh_runtime_paths()
    view = settings_module.SettingsView(user, session, "light")
    try:
        current = str(database.get_user_data_dir())
        view._data_folder_edit.setText(current)               # same folder: no switch
        view._theme_cb.setCurrentText("Oscuro")
        view._backup_interval_spin.setValue(12)
        view._backup_folder_edit.setText(str(tmp_path / "copias"))
        view._save_settings()
        saved = SettingsManager().load()
        assert (saved.theme, saved.backup_interval_hours) == ("dark", 12)
        assert str(database.get_user_data_dir()) == current

        # «Crear respaldo ahora» writes a zip into the backup folder.
        view._create_backup()
        zips = list((tmp_path / "copias").glob("profin_backup_maria_*.zip"))
        assert len(zips) == 1, dialogs

        # «Restaurar respaldo»: the chosen file goes to restore_all_data.
        restored = []
        monkeypatch.setattr(settings_module.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(zips[0]), "")))
        monkeypatch.setattr(view._settings_manager, "restore_all_data",
                            lambda p: restored.append(p) or True)
        view._restore_backup()
        assert restored == [str(zips[0])]
        monkeypatch.setattr(settings_module.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: ("", "")))
        view._restore_backup()                                  # cancelled: nothing
        assert len(restored) == 1
    finally:
        view.close()
        from conftest import point_app_at_test_data_dir
        point_app_at_test_data_dir()
        database.refresh_runtime_paths()


def test_settings_view_pages_and_folder_picker(qt_app, session, tmp_path, dialogs, monkeypatch):
    from app.gui import settings_view as settings_module

    user = create_user(session, "maria", "clave-secreta-1")
    view = settings_module.SettingsView(user, session, "light")
    try:
        for button in view.findChildren(settings_module.QPushButton):
            if button.property("page_key"):
                view._on_nav_click(button)
        monkeypatch.setattr(settings_module.QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: str(tmp_path)))
        view._browse_folder(view._import_folder_edit)
        assert view._import_folder_edit.text() == str(tmp_path)

        # Detect: a folder with a database that has users is offered.
        found = tmp_path / "encontrada"
        found.mkdir()
        engine = create_engine(f"sqlite:///{found / 'profin.db'}")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as s:
            create_user(s, "alguien", "clave-larga-1")
        engine.dispose()
        monkeypatch.setattr(settings_module, "get_legacy_data_dir", lambda: tmp_path / "vacia")
        monkeypatch.setattr(settings_module, "get_default_data_dir", lambda: found)
        view._detect_data_folder()
        assert view._data_folder_edit.text() == str(found)
        monkeypatch.setattr(settings_module, "get_default_data_dir", lambda: tmp_path / "vacia")
        view._detect_data_folder()
        assert dialogs[-1][1] == "Base de datos no encontrada"
        view.apply_theme("dark")
    finally:
        view.close()


# ---------------------------------------------------------------------------
# Categorías
# ---------------------------------------------------------------------------

def _select(view, name):
    """Select the tree item with this text (expanding as needed)."""
    model = view._tree_model
    for r in range(model.rowCount()):
        parent = model.item(r)
        if parent.text() == name:
            view._tree.setCurrentIndex(parent.index())
            return parent
        for c in range(parent.rowCount()):
            child = parent.child(c)
            if child.text() == name:
                view._tree.setCurrentIndex(child.index())
                return child
    raise AssertionError(f"{name} not in the tree")


def test_categories_view_crud_and_nature(qt_app, session, dialogs, monkeypatch):
    from app.gui import categories_view as cat_module

    user = create_user(session, "maria", "clave-secreta-1")
    other = create_user(session, "comun", "clave-secreta-2")
    gasto = Category(user_id=user.id, name="GASTO", level=CategoryLevel.type,
                     transaction_type=TransactionType.expense)
    session.add(gasto)
    session.commit()
    view = cat_module.CategoriesView(user, session, "light")
    try:
        names = iter(["Luz", "Electricidad"])
        monkeypatch.setattr(cat_module.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: (next(names), True)))
        _select(view, "GASTO")
        assert view._btn_add_sub.isEnabled() and not view._btn_delete.isEnabled()
        view._on_add_sub()
        luz = session.query(Category).filter_by(user_id=user.id, name="Luz").one()

        _select(view, "Luz")
        assert view._cmb_nature.isEnabled()
        view._cmb_nature.setCurrentIndex(view._cmb_nature.findData(CATEGORY_NATURE_FIXED))
        session.refresh(luz)
        assert luz.nature == CATEGORY_NATURE_FIXED
        assert not view._cmb_peer.isVisibleTo(view)             # «Con» only for a traspaso
        view._cmb_nature.setCurrentIndex(view._cmb_nature.findData(CATEGORY_NATURE_TRANSFER))
        assert view._cmb_peer.isVisibleTo(view)
        view._cmb_peer.setCurrentIndex(view._cmb_peer.findData(other.id))
        session.refresh(luz)
        assert (luz.nature, luz.transfer_peer_user_id) == (CATEGORY_NATURE_TRANSFER, other.id)

        view._on_rename()
        session.refresh(luz)
        assert luz.name == "Electricidad"

        _select(view, "Electricidad")
        view._on_delete()
        assert session.query(Category).filter_by(user_id=user.id, name="Electricidad").count() == 0
    finally:
        view.close()


def test_categories_view_defaults_and_export(qt_app, session, tmp_path, dialogs, monkeypatch):
    from app.gui import categories_view as cat_module

    user = create_user(session, "maria", "clave-secreta-1")
    view = cat_module.CategoriesView(user, session, "light")
    try:
        view._on_seed()
        assert session.query(Category).filter_by(user_id=user.id).count() > 10
        assert session.query(AutoCategorizationRule).filter_by(user_id=user.id).count() > 0
        assert view._tree_model.rowCount() >= 4

        target = tmp_path / "biblioteca.profin-cats.json"
        monkeypatch.setattr(cat_module.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        view._on_export()
        assert target.exists() and target.stat().st_size > 100
        view.apply_theme("dark")
    finally:
        view.close()


# ---------------------------------------------------------------------------
# Acceso web (with a fake server: no port, no certificates)
# ---------------------------------------------------------------------------

class _FakeSessions:
    def __init__(self):
        from types import SimpleNamespace
        import time
        self.items = [SimpleNamespace(username="ana", user_agent="Mozilla (iPad)", ip="192.168.1.9",
                                      last_seen=time.time() - 120)]

    def active(self):
        return list(self.items)

    def revoke_all(self):
        count, self.items = len(self.items), []
        return count


class _FakeServer:
    def __init__(self, can_start=True):
        from types import SimpleNamespace
        self.running = False
        self.error = None if can_start else "puerto ocupado"
        self.can_start = can_start
        self.material = None
        self.onboarding_active = False
        self.onboarding_until = None
        self.sessions = _FakeSessions()
        self.calls = []
        self._material = SimpleNamespace(ca_fingerprint="AA:BB", server_expires=date(2027, 1, 1))

    def start(self, port):
        self.calls.append(("start", port))
        self.running = self.can_start
        self.material = self._material if self.running else None
        return self.running

    def stop(self):
        self.calls.append(("stop",))
        self.running = False

    def urls(self):
        return ["https://192.168.1.20:8765"]

    def start_onboarding(self):
        import time
        self.onboarding_active, self.onboarding_until = True, time.time() + 600
        return True

    def stop_onboarding(self):
        self.onboarding_active = False

    def onboarding_url(self):
        return "http://192.168.1.20:8766/configurar"

    def regenerate_certificates(self):
        self.calls.append(("regenerate",))


def _web_page(monkeypatch, server):
    from app.gui import web_access_view as module
    from app.web.server import WebSettings

    stored = []
    monkeypatch.setattr(module, "get_web_server", lambda: server)
    monkeypatch.setattr(module, "load_web_settings", lambda: WebSettings(enabled=False, port=8765))
    monkeypatch.setattr(module, "save_web_settings", lambda s: stored.append(s))
    return module.WebAccessPage(), stored


def test_web_access_page_on_off_onboarding_and_sessions(qt_app, dialogs, monkeypatch):
    server = _FakeServer()
    page, stored = _web_page(monkeypatch, server)
    try:
        assert "Desactivado" in page._status.text()
        page._enabled_cb.setChecked(True)                      # → _on_toggle(True)
        assert server.calls[0] == ("start", 8765) and stored[-1].enabled
        assert "Activo" in page._status.text()
        assert "192.168.1.20:8765" in page._urls.text() and page._qr_for
        assert "AA:BB" in page._fingerprint.text()
        assert page._sessions.item(0).text().startswith("ana · iPad")

        page._toggle_onboarding()
        assert server.onboarding_active and "configurar" in page._onb_label.text()
        page._toggle_onboarding()
        assert not server.onboarding_active

        page._regenerate()                                      # question → Yes
        assert ("regenerate",) in server.calls
        page._revoke_all()
        assert page._sessions.item(0).text() == "Ninguna"

        page._port_spin.setValue(9000)
        page._apply_port()
        assert ("start", 9000) in server.calls
        page._enabled_cb.setChecked(False)
        assert ("stop",) in server.calls and "Desactivado" in page._status.text()
    finally:
        page.close()


def test_web_access_page_reports_a_failed_start(qt_app, dialogs, monkeypatch):
    server = _FakeServer(can_start=False)
    page, _stored = _web_page(monkeypatch, server)
    try:
        page._enabled_cb.setChecked(True)
        assert dialogs[-1][0] == "warning" and "puerto ocupado" in dialogs[-1][2]
        assert "Detenido" in page._status.text()
        # Onboarding with the server off and refusing to start: no setup page.
        page._enabled_cb.setChecked(False)
        page._toggle_onboarding()
        assert not server.onboarding_active
    finally:
        page.close()


# ---------------------------------------------------------------------------
# Movimientos: the category / «Gasto fijo» dialog and the Fijo filter
# ---------------------------------------------------------------------------

@pytest.fixture
def movements(session):
    from app.db.models import CATEGORY_SOURCE_RULE

    user = create_user(session, "maria", "clave-secreta-1")
    account = Account(user_id=user.id, account_name="Cuenta")
    gasto = Category(user_id=user.id, name="GASTO", level=CategoryLevel.type,
                     transaction_type=TransactionType.expense)
    session.add_all([account, gasto])
    session.flush()
    luz = Category(user_id=user.id, parent_id=gasto.id, name="Luz", level=CategoryLevel.category,
                   nature=CATEGORY_NATURE_FIXED)
    ocio = Category(user_id=user.id, parent_id=gasto.id, name="Ocio", level=CategoryLevel.category)
    session.add_all([luz, ocio])
    session.flush()

    def txn(day, issuer, cat=None, source=None):
        t = Transaction(user_id=user.id, account_id=account.id, transaction_date=date(2026, 9, day),
                        amount=-20.0, type=TransactionType.expense, issuer_name=issuer,
                        category_id=cat.id if cat else None, category_source=source)
        session.add(t)
        return t

    rows = {"luz": txn(1, "IBERDROLA", luz, CATEGORY_SOURCE_RULE),
            "cine1": txn(2, "CINE"), "cine2": txn(3, "CINE"), "cine3": txn(4, "CINE")}
    session.commit()
    return user, {"luz": luz, "ocio": ocio}, rows


def _answer_dialog(monkeypatch, category=None, fixed="keep", tick_similar=False):
    """Replace QDialog.exec: set the category / Gasto fijo combos, maybe tick
    «aplicar a iguales», then accept."""
    from PySide6.QtWidgets import QCheckBox, QComboBox

    def fake_exec(dlg):
        cmb, cmb_fixed = dlg.findChildren(QComboBox)[:2]
        if category is not None:
            cmb.setCurrentIndex(cmb.findData(category))
        if fixed != "keep":
            cmb_fixed.setCurrentIndex(cmb_fixed.findData(fixed))
        for box in dlg.findChildren(QCheckBox):
            if "iguales" in box.text() and "Aplicar" in box.text():
                box.setChecked(tick_similar)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)


def _row_of(view, txn_id):
    return view._txn_ids.index(txn_id)


def test_transactions_dialog_changing_only_fijo_keeps_the_rule_category(qt_app, session, movements, monkeypatch):
    from app.db.models import CATEGORY_SOURCE_RULE, FIXED_OVERRIDE_VARIABLE
    from app.gui.transactions_view import TransactionsView

    user, cats, rows = movements
    view = TransactionsView(user, session, "light")
    try:
        view._load_data()
        luz = rows["luz"]
        assert view._model.item(_row_of(view, luz.id), 5).toolTip() == "Gasto fijo"
        _answer_dialog(monkeypatch, fixed=FIXED_OVERRIDE_VARIABLE)
        view._open_category_dialog(luz.id, _row_of(view, luz.id))
        session.refresh(luz)
        assert luz.fixed_override == FIXED_OVERRIDE_VARIABLE
        assert luz.category_source == CATEGORY_SOURCE_RULE         # not locked as manual
        assert luz.category_id == cats["luz"].id
    finally:
        view.close()


def test_transactions_dialog_assigns_a_category_to_similar_ones(qt_app, session, movements, monkeypatch):
    from app.db.models import CATEGORY_SOURCE_MANUAL
    from app.gui.transactions_view import TransactionsView

    user, cats, rows = movements
    view = TransactionsView(user, session, "light")
    try:
        view._load_data()
        first = rows["cine1"]
        _answer_dialog(monkeypatch, category=cats["ocio"].id, tick_similar=True)
        view._open_category_dialog(first.id, _row_of(view, first.id))
        for key in ("cine1", "cine2", "cine3"):
            session.refresh(rows[key])
            assert (rows[key].category_id, rows[key].category_source) == (cats["ocio"].id, CATEGORY_SOURCE_MANUAL)
        assert view._model.item(_row_of(view, first.id), 5).text() == "Ocio"
    finally:
        view.close()


def test_transactions_fijo_filter(qt_app, session, movements):
    from app.gui.transactions_view import TransactionsView

    user, _cats, rows = movements
    view = TransactionsView(user, session, "light")
    try:
        view._cmb_nature.setCurrentIndex(view._cmb_nature.findData("fijo"))
        view._load_data()
        assert view._txn_ids == [rows["luz"].id]
        view._cmb_nature.setCurrentIndex(view._cmb_nature.findData("variable"))
        view._load_data()
        assert set(view._txn_ids) == {rows["cine1"].id, rows["cine2"].id, rows["cine3"].id}
    finally:
        view.close()
