"""
app/gui/login_window.py
========================
FAMILYFIN — Login Window

Features:
  - Fullscreen on launch (showMaximized)
  - Auto-fills last logged-in username from QSettings
  - Password field always empty (security)
  - Light/dark theme toggle (persisted)
  - Modern card-based layout centred on screen
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore    import Qt, QSettings
from PySide6.QtGui     import QFont, QColor, QPalette, QIcon, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QCheckBox, QFrame,
    QApplication, QMessageBox, QSizePolicy, QFileDialog, QComboBox,
)
import qtawesome as qta

from app.config        import APP_NAME, APP_VERSION, THEMES
from app.db import database as database_module
from app.db.database   import (
    Base,
    get_database_users,
    get_default_data_dir,
    get_session,
    get_user_data_dir,
    refresh_runtime_paths,
)
from app.auth.auth_service import verify_login
from app.settings      import SettingsManager


class _ElidedPathLabel(QLabel):
    """
    Non-interactive display for a filesystem path: elides from the front
    (…\\…\\data) to fit the available width instead of clipping, keeps the
    full path in the tooltip, and re-elides on resize.
    """

    def __init__(self) -> None:
        super().__init__()
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set_path(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._update_elided_text()

    def full_path(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self) -> None:
        if not self._full_text:
            return
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(
            self._full_text, Qt.TextElideMode.ElideLeft, self.width()
        )
        super().setText(elided)


class LoginWindow(QWidget):
    """
    Full-screen login window.
    Emits no signals — on success it instantiates MainWindow and closes self.
    """

    SETTINGS_ORG  = "GEVernova"
    SETTINGS_APP  = "FAMILYFIN"
    KEY_LAST_USER = "auth/last_username"
    KEY_THEME     = "ui/theme"

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        saved_settings = SettingsManager().load()
        remember_theme = saved_settings.save_appearance_on_start
        startup_theme = self._settings.value(self.KEY_THEME, saved_settings.theme if remember_theme else "light")
        self._theme = startup_theme if startup_theme in THEMES else (saved_settings.theme if remember_theme else "light")
        if not remember_theme:
            self._settings.setValue(self.KEY_THEME, "light")
        self._data_dir = get_user_data_dir()
        self._session  = get_session()

        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")

        self._build_ui()
        self._apply_theme()
        self._refresh_database_info()
        self._load_last_user()

        self.showMaximized()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ────────────────────────────────────────────────────
        self._top_bar = QFrame()
        self._top_bar.setFixedHeight(48)
        top_layout = QHBoxLayout(self._top_bar)
        top_layout.setContentsMargins(24, 0, 24, 0)

        lbl_brand = QLabel(f"<b>{APP_NAME}</b>")
        lbl_brand.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        top_layout.addWidget(lbl_brand)
        top_layout.addStretch()

        # Theme toggle
        self._btn_theme = QPushButton()
        self._btn_theme.setFixedSize(36, 36)
        self._btn_theme.setFlat(True)
        self._btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_theme.setToolTip("Cambiar tema claro/oscuro")
        self._btn_theme.clicked.connect(self._toggle_theme)
        top_layout.addWidget(self._btn_theme)

        root.addWidget(self._top_bar)

        # ── Centre area ────────────────────────────────────────────────
        centre = QWidget()
        centre.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Expanding)
        centre_layout = QVBoxLayout(centre)
        centre_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(centre)

        # Card
        self._card = QFrame()
        self._card.setFixedWidth(420)
        self._card.setObjectName("loginCard")
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(40, 40, 40, 40)
        card_layout.setSpacing(20)
        centre_layout.addWidget(self._card)

        # App icon + title
        lbl_icon = QLabel()
        lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_pixmap = qta.icon(
            "mdi.chart-areaspline", color="#2563EB"
        ).pixmap(56, 56)
        lbl_icon.setPixmap(icon_pixmap)
        card_layout.addWidget(lbl_icon)

        lbl_title = QLabel(APP_NAME)
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        lbl_title.setObjectName("loginTitle")
        card_layout.addWidget(lbl_title)

        lbl_sub = QLabel("Personal & Project Finance Tracker")
        lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_sub.setObjectName("loginSubtitle")
        card_layout.addWidget(lbl_sub)

        card_layout.addSpacing(8)

        # Username
        lbl_user = QLabel("Usuario")
        lbl_user.setObjectName("fieldLabel")
        card_layout.addWidget(lbl_user)

        self._user_combo = QComboBox()
        self._user_combo.setPlaceholderText("Selecciona un usuario")
        self._user_combo.setFixedHeight(42)
        self._user_combo.setObjectName("loginInput")
        card_layout.addWidget(self._user_combo)

        # Password
        lbl_pass = QLabel("Contraseña")
        lbl_pass.setObjectName("fieldLabel")
        card_layout.addWidget(lbl_pass)

        self._inp_pass = QLineEdit()
        self._inp_pass.setPlaceholderText("Contraseña")
        self._inp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_pass.setFixedHeight(42)
        self._inp_pass.setObjectName("loginInput")
        self._inp_pass.returnPressed.connect(self._on_login)
        card_layout.addWidget(self._inp_pass)

        # Database folder
        lbl_data = QLabel("Base de datos")
        lbl_data.setObjectName("fieldLabel")
        card_layout.addWidget(lbl_data)

        data_row = QHBoxLayout()
        self._data_folder = _ElidedPathLabel()
        self._data_folder.setFixedHeight(42)
        self._data_folder.set_path(str(self._data_dir))
        self._data_folder.setObjectName("loginPathDisplay")
        data_row.addWidget(self._data_folder)

        self._btn_data_folder = QPushButton("Buscar BD")
        self._btn_data_folder.setFixedHeight(42)
        self._btn_data_folder.setObjectName("btnSecondary")
        self._btn_data_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_data_folder.clicked.connect(self._select_data_folder)
        data_row.addWidget(self._btn_data_folder)
        card_layout.addLayout(data_row)

        self._database_status = QLabel()
        self._database_status.setWordWrap(True)
        self._database_status.setObjectName("databaseStatus")
        card_layout.addWidget(self._database_status)

        # Error label (hidden by default)
        self._lbl_error = QLabel("")
        self._lbl_error.setObjectName("loginError")
        self._lbl_error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_error.setVisible(False)
        card_layout.addWidget(self._lbl_error)

        # Login button
        self._btn_login = QPushButton("  Iniciar Sesión")
        self._btn_login.setIcon(
            qta.icon("mdi.login", color="#FFFFFF")
        )
        self._btn_login.setFixedHeight(44)
        self._btn_login.setObjectName("btnPrimary")
        self._btn_login.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_login.clicked.connect(self._on_login)
        card_layout.addWidget(self._btn_login)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setObjectName("loginDivider")
        card_layout.addWidget(div)

        # Register link
        self._btn_register = QPushButton("  ¿Primera vez? Crear cuenta")
        self._btn_register.setIcon(
            qta.icon("mdi.account-plus-outline", color="#2563EB")
        )
        self._btn_register.setFixedHeight(38)
        self._btn_register.setObjectName("btnSecondary")
        self._btn_register.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_register.clicked.connect(self._on_register)
        card_layout.addWidget(self._btn_register)

        card_layout.addSpacing(8)

        # Version footer
        lbl_ver = QLabel(f"v{APP_VERSION}")
        lbl_ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_ver.setObjectName("loginVersion")
        card_layout.addWidget(lbl_ver)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _refresh_database_info(self) -> None:
        """Show database status and populate users from the active directory."""
        database_path = self._data_dir / "profin.db"
        self._user_combo.clear()
        if not database_path.is_file():
            self._database_status.setText("BD no detectada en este directorio.")
            self._database_status.setProperty("databaseState", "missing")
            self._refresh_status_style()
            return

        try:
            users = get_database_users(self._data_dir)
            modified = datetime.fromtimestamp(
                database_path.stat().st_mtime
            ).strftime("%d/%m/%Y %H:%M:%S")
        except (OSError, sqlite3.DatabaseError) as exc:
            self._database_status.setText(f"No se pudo leer la BD: {exc}")
            self._database_status.setProperty("databaseState", "invalid")
            self._refresh_status_style()
            return

        self._database_status.setText(
            f"BD encontrada. Última modificación: {modified}"
        )
        self._database_status.setProperty("databaseState", "found")
        self._user_combo.addItems(users)
        self._refresh_status_style()

    def _refresh_status_style(self) -> None:
        self._database_status.style().unpolish(self._database_status)
        self._database_status.style().polish(self._database_status)

    def _select_data_folder(self) -> None:
        """Select a database folder, including an empty one for registration."""
        selected = QFileDialog.getExistingDirectory(
            self,
            "Seleccionar carpeta de la base de datos",
            self._data_folder.full_path() or str(get_default_data_dir()),
        )
        if not selected:
            return

        data_dir = Path(selected).expanduser().resolve()
        previous_value = self._settings.value("settings/data_folder", "", type=str)
        try:
            self._session.close()
            self._settings.setValue("settings/data_folder", str(data_dir))
            self._settings.sync()
            self._data_dir = refresh_runtime_paths()
            self._session = get_session()
            self._data_folder.set_path(str(self._data_dir))
            self._refresh_database_info()
            self._lbl_error.setVisible(False)
        except (OSError, sqlite3.DatabaseError) as exc:
            self._settings.setValue("settings/data_folder", previous_value)
            self._settings.sync()
            refresh_runtime_paths()
            self._session = get_session()
            self._show_error(f"No se pudo activar la base de datos:\n{exc}")

    def _on_login(self) -> None:
        username = self._user_combo.currentText().strip()
        password = self._inp_pass.text()

        if not username:
            self._show_error("Por favor introduce tu nombre de usuario.")
            return
        if not password:
            self._show_error("Por favor introduce tu contraseña.")
            return

        user = verify_login(self._session, username, password)

        if user is None:
            self._show_error("Usuario o contraseña incorrectos.")
            self._inp_pass.clear()
            self._inp_pass.setFocus()
            return

        # Persist last user
        self._settings.setValue(self.KEY_LAST_USER, username)
        self._settings.setValue(self.KEY_THEME,     self._theme)

        # Launch main window
        from app.gui.main_window import MainWindow
        self._main = MainWindow(user=user, session=self._session,
                                theme=self._theme)
        self._main.show()
        self.close()

    def _on_register(self) -> None:
        """Open the inline register dialog."""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        t = THEMES[self._theme]

        dlg = QDialog(self)
        dlg.setWindowTitle("Crear nueva cuenta")
        dlg.setFixedWidth(360)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        lbl_title = QLabel("Crear nueva cuenta")
        lbl_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)

        lbl_u = QLabel("Usuario")
        lbl_u.setObjectName("fieldLabel")
        layout.addWidget(lbl_u)

        inp_user = QLineEdit()
        inp_user.setPlaceholderText("Elige un nombre de usuario")
        inp_user.setFixedHeight(40)
        inp_user.setObjectName("loginInput")
        layout.addWidget(inp_user)

        lbl_p = QLabel("Contraseña")
        lbl_p.setObjectName("fieldLabel")
        layout.addWidget(lbl_p)

        inp_pass = QLineEdit()
        inp_pass.setPlaceholderText("Elige una contraseña")
        inp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        inp_pass.setFixedHeight(40)
        inp_pass.setObjectName("loginInput")
        layout.addWidget(inp_pass)

        lbl_p2 = QLabel("Confirmar contraseña")
        lbl_p2.setObjectName("fieldLabel")
        layout.addWidget(lbl_p2)

        inp_pass2 = QLineEdit()
        inp_pass2.setPlaceholderText("Repite la contraseña")
        inp_pass2.setEchoMode(QLineEdit.EchoMode.Password)
        inp_pass2.setFixedHeight(40)
        inp_pass2.setObjectName("loginInput")
        layout.addWidget(inp_pass2)

        lbl_err = QLabel("")
        lbl_err.setObjectName("loginError")
        lbl_err.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_err.setVisible(False)
        layout.addWidget(lbl_err)

        btn_box = QDialogButtonBox()
        btn_create = btn_box.addButton(
            "Crear cuenta", QDialogButtonBox.ButtonRole.AcceptRole
        )
        btn_create.setObjectName("btnPrimary")
        btn_create.setFixedHeight(40)
        btn_cancel = btn_box.addButton(
            "Cancelar", QDialogButtonBox.ButtonRole.RejectRole
        )
        btn_cancel.setObjectName("btnSecondary")
        btn_cancel.setFixedHeight(40)
        layout.addWidget(btn_box)

        dlg.setStyleSheet(self.styleSheet())

        def _try_create() -> None:
            from app.auth.auth_service import create_user
            username = inp_user.text().strip()
            password = inp_pass.text()
            password2 = inp_pass2.text()

            if not username:
                lbl_err.setText("El nombre de usuario no puede estar vacío.")
                lbl_err.setVisible(True)
                return
            if not password:
                lbl_err.setText("La contraseña no puede estar vacía.")
                lbl_err.setVisible(True)
                return
            if password != password2:
                lbl_err.setText("Las contraseñas no coinciden.")
                lbl_err.setVisible(True)
                inp_pass2.clear()
                return
            if len(password) < 6:
                lbl_err.setText("La contraseña debe tener al menos 6 caracteres.")
                lbl_err.setVisible(True)
                return
            try:
                Base.metadata.create_all(bind=database_module.engine)
                create_user(self._session, username, password)
                dlg.accept()
                # Auto-fill username after successful registration
                if self._user_combo.findText(username) < 0:
                    self._user_combo.addItem(username)
                self._user_combo.setCurrentText(username)
                self._inp_pass.setFocus()
                self._show_error(
                    f"✓ Cuenta '{username}' creada. Introduce tu contraseña para entrar."
                )
                self._lbl_error.setStyleSheet(
                    f"color: {t['income']}; font-size: 12px;"
                )
            except ValueError as exc:
                lbl_err.setText(str(exc))
                lbl_err.setVisible(True)

        btn_box.accepted.connect(_try_create)
        btn_box.rejected.connect(dlg.reject)
        inp_pass2.returnPressed.connect(_try_create)

        dlg.exec()

    def _toggle_theme(self) -> None:
        self._theme = "dark" if self._theme == "light" else "light"
        self._apply_theme()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_error(self, msg: str) -> None:
        self._lbl_error.setText(msg)
        self._lbl_error.setVisible(True)

    def _load_last_user(self) -> None:
        last = self._settings.value(self.KEY_LAST_USER, "")
        if last and self._user_combo.findText(last) >= 0:
            self._user_combo.setCurrentText(last)
            self._inp_pass.setFocus()
        else:
            self._user_combo.setFocus()

    def _apply_theme(self) -> None:
        t = THEMES[self._theme]
        is_dark = self._theme == "dark"

        # Theme button icon
        icon_name = "mdi.weather-night" if self._theme == "light" else "mdi.weather-sunny"
        self._btn_theme.setIcon(
            qta.icon(icon_name, color=t["text_primary"])
        )

        self.setStyleSheet(f"""
            QWidget {{
                background-color: {t["bg_primary"]};
                color: {t["text_primary"]};
                font-family: "Segoe UI";
                font-size: 13px;
            }}
            QFrame#loginCard {{
                background-color: {t["bg_secondary"]};
                border-radius: 16px;
                border: 1px solid {t["border"]};
            }}
            QLabel#loginTitle {{
                color: {t["text_primary"]};
                font-size: 24px;
                font-weight: 700;
            }}
            QLabel#loginSubtitle {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QLabel#fieldLabel {{
                color: {t["text_secondary"]};
                font-size: 12px;
                font-weight: 600;
                margin-bottom: 2px;
            }}
            QLineEdit#loginInput {{
                background-color: {t["bg_primary"]};
                border: 1.5px solid {t["border"]};
                border-radius: 8px;
                padding: 8px 12px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QLineEdit#loginInput:focus {{
                border: 1.5px solid {t["accent"]};
            }}
            QComboBox#loginInput {{
                background-color: {t["bg_primary"]};
                border: 1.5px solid {t["border"]};
                border-radius: 8px;
                padding: 8px 12px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QLabel#loginPathDisplay {{
                background-color: {t["bg_primary"]};
                border: 1.5px solid {t["border"]};
                border-radius: 8px;
                padding: 8px 12px;
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QLabel#databaseStatus {{
                color: {t["text_secondary"]};
                font-size: 11px;
            }}
            QLabel#databaseStatus[databaseState="found"] {{
                color: {t["income"]};
            }}
            QLabel#databaseStatus[databaseState="missing"],
            QLabel#databaseStatus[databaseState="invalid"] {{
                color: {t["expense"]};
            }}
            QPushButton#btnPrimary {{
                background-color: {t["accent"]};
                color: #FFFFFF;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton#btnPrimary:hover {{
                background-color: {t["accent_hover"]};
            }}
            QPushButton#btnPrimary:pressed {{
                background-color: {t["accent_hover"]};
            }}
            QLabel#loginError {{
                color: {t["expense"]};
                font-size: 12px;
            }}
            QLabel#loginVersion {{
                color: {t["text_secondary"]};
                font-size: 11px;
            }}
            QPushButton#btnSecondary {{
                background-color: transparent;
                color: {t["accent"]};
                border: 1.5px solid {t["border"]};
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
                padding: 0 16px;
            }}
            QPushButton#btnSecondary:hover {{
                background-color: {t["accent_light"]};
                border-color: {t["accent"]};
            }}
            QFrame#loginDivider {{
                color: {t["border"]};
                background-color: {t["border"]};
                max-height: 1px;
                margin: 4px 0px;
            }}
            QFrame#topBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
        """)

        self._top_bar.setStyleSheet(
            f"background-color: {t['bg_secondary']};"
            f"border-bottom: 1px solid {t['border']};"
        )