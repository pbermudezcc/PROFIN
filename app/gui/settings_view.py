from __future__ import annotations

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QComboBox,
    QCheckBox,
    QLineEdit,
    QSpinBox,
    QGroupBox,
    QFormLayout,
    QStackedWidget,
    QButtonGroup,
    QSizePolicy,
    QFileDialog,
    QMessageBox,
)
import logging
from pathlib import Path

import qtawesome as qta

from app.config import THEMES
from app.db.database import (
    database_has_users,
    get_default_data_dir,
    get_legacy_data_dir,
    get_user_data_dir,
    init_db,
)
from app.settings import SettingsManager
from app.gui._style_utils import apply_card_shadow

logger = logging.getLogger(__name__)


class SettingsView(QWidget):
    """Professional settings screen with a left navigation and right-side forms."""

    def __init__(self, user, session, theme: str = "light") -> None:
        super().__init__()
        self._user = user
        self._session = session
        self._theme = theme
        self._t = THEMES[theme]
        self._settings_manager = SettingsManager()
        self._settings = self._settings_manager.load()
        self._page_map = {}
        self._page_names = [
            ("general", "General"),
            ("appearance", "Apariencia"),
            ("data", "Datos"),
            ("security", "Seguridad"),
            ("backup", "Copias de seguridad"),
        ]
        self._build_ui()
        self._apply_theme()
        self._load_into_form()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QFrame()
        top.setObjectName("dashTopBar")
        top.setFixedHeight(56)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(24, 0, 24, 0)
        top_layout.setSpacing(12)

        lbl_title = QLabel("Configuración")
        lbl_title.setObjectName("dashTitle")
        top_layout.addWidget(lbl_title)
        top_layout.addStretch()

        self._btn_apply = QPushButton("Guardar")
        self._btn_apply.setObjectName("btnPrimary")
        self._btn_apply.setFixedHeight(34)
        self._btn_apply.clicked.connect(self._save_settings)
        top_layout.addWidget(self._btn_apply)

        root.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(24, 18, 24, 24)
        body.setSpacing(18)

        nav = QFrame()
        nav.setObjectName("settingsSidebar")
        nav.setFixedWidth(220)
        self._nav_panel = nav
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(12, 12, 12, 12)
        nav_layout.setSpacing(6)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        for key, label in self._page_names:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(38)
            btn.setObjectName("settingsNavButton")
            btn.setProperty("page_key", key)
            self._nav_group.addButton(btn)
            nav_layout.addWidget(btn)
            if key == "general":
                btn.setChecked(True)

        nav_layout.addStretch()
        body.addWidget(nav)

        stack = QStackedWidget()
        stack.setObjectName("settingsStack")
        self._stack = stack

        self._page_map["general"] = self._build_general_page()
        self._page_map["appearance"] = self._build_appearance_page()
        self._page_map["data"] = self._build_data_page()
        self._page_map["security"] = self._build_security_page()
        self._page_map["backup"] = self._build_backup_page()

        for key in self._page_names:
            stack.addWidget(self._page_map[key[0]])

        self._nav_group.buttonClicked.connect(self._on_nav_click)
        body.addWidget(stack, 1)
        root.addLayout(body)

    def _on_nav_click(self, button: QPushButton) -> None:
        key = button.property("page_key")
        self._stack.setCurrentIndex(self._page_names.index(next(item for item in self._page_names if item[0] == key)))

    def _build_general_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        group = QGroupBox("General")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._language_cb = QComboBox()
        self._language_cb.addItems(["Español", "English"])
        form.addRow("Idioma", self._language_cb)

        self._default_period_cb = QComboBox()
        self._default_period_cb.addItems(["Mes actual", "Mes anterior", "Últimos 3 meses", "Año actual", "Todo"])
        form.addRow("Período por defecto", self._default_period_cb)

        self._currency_cb = QComboBox()
        self._currency_cb.addItems(["EUR (€)", "USD ($)", "GBP (£)"])
        form.addRow("Moneda principal", self._currency_cb)

        layout.addWidget(group)
        return page

    def _build_appearance_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        group = QGroupBox("Apariencia")
        form = QFormLayout(group)

        self._theme_cb = QComboBox()
        self._theme_cb.addItems(["Claro", "Oscuro"])
        form.addRow("Tema", self._theme_cb)

        self._save_appearance_start_cb = QCheckBox("Guardar apariencia al iniciar")
        form.addRow("Inicio", self._save_appearance_start_cb)

        self._show_hints_cb = QCheckBox("Mostrar consejos de uso")
        form.addRow("Ayuda contextual", self._show_hints_cb)

        layout.addWidget(group)
        return page

    def _build_data_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        group = QGroupBox("Datos e importación")
        form = QFormLayout(group)

        self._import_folder_edit = QLineEdit()
        self._import_folder_edit.setPlaceholderText("Ruta de importación")
        import_row = QHBoxLayout()
        import_row.addWidget(self._import_folder_edit)
        import_btn = QPushButton("Explorar")
        import_btn.clicked.connect(lambda: self._browse_folder(self._import_folder_edit))
        import_row.addWidget(import_btn)
        form.addRow("Carpeta de importación", import_row)

        self._export_folder_edit = QLineEdit()
        self._export_folder_edit.setPlaceholderText("Ruta de exportación")
        export_row = QHBoxLayout()
        export_row.addWidget(self._export_folder_edit)
        export_btn = QPushButton("Explorar")
        export_btn.clicked.connect(lambda: self._browse_folder(self._export_folder_edit))
        export_row.addWidget(export_btn)
        form.addRow("Carpeta de exportación", export_row)

        self._data_folder_edit = QLineEdit()
        self._data_folder_edit.setPlaceholderText("Ruta de datos del usuario")
        data_row = QHBoxLayout()
        data_row.addWidget(self._data_folder_edit)
        data_btn = QPushButton("Explorar")
        data_btn.clicked.connect(lambda: self._browse_folder(self._data_folder_edit))
        data_row.addWidget(data_btn)
        self._btn_detect_data = QPushButton("Detectar BD")
        self._btn_detect_data.clicked.connect(self._detect_data_folder)
        data_row.addWidget(self._btn_detect_data)
        self._btn_move_data = QPushButton("Migrar aquí")
        self._btn_move_data.clicked.connect(self._migrate_data_folder)
        data_row.addWidget(self._btn_move_data)
        form.addRow("Carpeta de datos del usuario", data_row)

        layout.addWidget(group)
        return page

    def _build_security_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        group = QGroupBox("Seguridad")
        form = QFormLayout(group)

        self._require_password_cb = QCheckBox("Solicitar contraseña al iniciar")
        self._protect_data_cb = QCheckBox("Proteger datos sensibles")
        form.addRow("Inicio de sesión", self._require_password_cb)
        form.addRow("Protección", self._protect_data_cb)

        layout.addWidget(group)
        return page

    def _build_backup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        group = QGroupBox("Copias de seguridad")
        form = QFormLayout(group)

        self._auto_backup_cb = QCheckBox("Hacer copia automática")
        form.addRow("Autocopia", self._auto_backup_cb)

        self._backup_interval_spin = QSpinBox()
        self._backup_interval_spin.setRange(1, 168)
        self._backup_interval_spin.setSuffix(" h")
        form.addRow("Frecuencia", self._backup_interval_spin)

        self._backup_folder_edit = QLineEdit()
        self._backup_folder_edit.setPlaceholderText("Ruta para guardar backups")
        backup_row = QHBoxLayout()
        backup_row.addWidget(self._backup_folder_edit)
        backup_btn = QPushButton("Explorar")
        backup_btn.clicked.connect(lambda: self._browse_folder(self._backup_folder_edit))
        backup_row.addWidget(backup_btn)
        form.addRow("Carpeta de backups", backup_row)

        self._weekly_summary_cb = QCheckBox("Enviar resumen semanal")
        form.addRow("Resumen", self._weekly_summary_cb)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        self._btn_backup_now = QPushButton("Crear respaldo ahora")
        self._btn_backup_now.setObjectName("btnPrimary")
        self._btn_backup_now.clicked.connect(self._create_backup)
        actions.addWidget(self._btn_backup_now)

        self._btn_restore_now = QPushButton("Restaurar respaldo")
        self._btn_restore_now.clicked.connect(self._restore_backup)
        actions.addWidget(self._btn_restore_now)
        actions.addStretch()
        layout.addLayout(actions)

        layout.addWidget(group)
        return page

    def _load_into_form(self) -> None:
        self._language_cb.setCurrentText("Español" if self._settings.language == "es" else "English")
        self._default_period_cb.setCurrentText(self._settings.default_period)
        self._currency_cb.setCurrentText({"EUR": "EUR (€)", "USD": "USD ($)", "GBP": "GBP (£)"}.get(self._settings.default_currency, "EUR (€)"))
        self._theme_cb.setCurrentText("Claro" if self._settings.theme == "light" else "Oscuro")
        self._save_appearance_start_cb.setChecked(self._settings.save_appearance_on_start)
        self._show_hints_cb.setChecked(self._settings.enable_hints)
        self._import_folder_edit.setText(self._settings.import_folder)
        self._export_folder_edit.setText(self._settings.export_folder)
        self._data_folder_edit.setText(self._settings.data_folder or str(get_default_data_dir()))
        self._backup_folder_edit.setText(self._settings.backup_folder)
        self._require_password_cb.setChecked(self._settings.require_password_on_start)
        self._protect_data_cb.setChecked(self._settings.protect_sensitive_data)
        self._auto_backup_cb.setChecked(self._settings.auto_backup)
        self._backup_interval_spin.setValue(self._settings.backup_interval_hours)
        self._weekly_summary_cb.setChecked(self._settings.weekly_summary_email)

    def _collect_from_form(self) -> None:
        settings = self._settings
        settings.language = "es" if self._language_cb.currentText() == "Español" else "en"
        settings.default_period = self._default_period_cb.currentText()
        settings.default_currency = {
            "EUR (€)": "EUR",
            "USD ($)": "USD",
            "GBP (£)": "GBP",
        }[self._currency_cb.currentText()]
        settings.theme = "light" if self._theme_cb.currentText() == "Claro" else "dark"
        settings.save_appearance_on_start = self._save_appearance_start_cb.isChecked()
        settings.enable_hints = self._show_hints_cb.isChecked()
        settings.import_folder = self._import_folder_edit.text().strip()
        settings.export_folder = self._export_folder_edit.text().strip()
        settings.data_folder = self._data_folder_edit.text().strip()
        settings.backup_folder = self._backup_folder_edit.text().strip()
        settings.require_password_on_start = self._require_password_cb.isChecked()
        settings.protect_sensitive_data = self._protect_data_cb.isChecked()
        settings.auto_backup = self._auto_backup_cb.isChecked()
        settings.backup_interval_hours = self._backup_interval_spin.value()
        settings.weekly_summary_email = self._weekly_summary_cb.isChecked()
        self._settings = settings

    def _browse_folder(self, field: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta", field.text() or ".")
        if folder:
            field.setText(folder)

    def _detect_data_folder(self) -> None:
        candidates = []
        for folder in (get_legacy_data_dir(), get_default_data_dir()):
            if database_has_users(folder / "profin.db"):
                candidates.append(folder)

        if not candidates:
            QMessageBox.information(
                self,
                "Base de datos no encontrada",
                "No se encontró una base de datos con usuarios válidos en las rutas conocidas del proyecto ni del usuario local.",
            )
            return

        chosen = sorted(candidates, key=lambda path: len(str(path))) [0]
        self._data_folder_edit.setText(str(chosen))
        QMessageBox.information(
            self,
            "Base de datos detectada",
            f"Se ha localizado la base de datos actual en:\n{chosen}",
        )

    def _save_settings(self) -> None:
        self._collect_from_form()
        # save() overwrites settings/data_folder, so remember the folder in use.
        current_data_dir = get_user_data_dir()
        self._settings_manager.ensure_dirs(self._settings)
        self._settings_manager.save(self._settings)

        qsettings = QSettings("GEVernova", "FAMILYFIN")
        qsettings.setValue("ui/theme", self._settings.theme if self._settings.save_appearance_on_start else "light")
        qsettings.sync()

        parent = self.parentWidget()
        while parent is not None and not hasattr(parent, "_theme"):
            parent = parent.parentWidget()
        if parent is not None:
            parent._theme = self._settings.theme
            parent._t = THEMES[self._settings.theme]
            if hasattr(parent, "_apply_theme"):
                parent._apply_theme()
            if hasattr(parent, "_views"):
                for view in parent._views.values():
                    if hasattr(view, "apply_theme"):
                        view.apply_theme(self._settings.theme)
                    elif hasattr(view, "refresh_theme"):
                        view.refresh_theme(self._settings.theme)
        self.apply_theme(self._settings.theme)

        if self._settings.data_folder:
            try:
                self._switch_data_folder(self._settings.data_folder, current_data_dir)
            except Exception as exc:
                logger.exception("No se pudo cambiar la carpeta de datos.")
                QMessageBox.critical(self, "Error al migrar datos", str(exc))

    def _migrate_data_folder(self) -> None:
        target = self._data_folder_edit.text().strip()
        if not target:
            QMessageBox.warning(self, "Ubicación no válida", "Selecciona una carpeta para la ubicación de datos del usuario.")
            return
        try:
            self._settings.data_folder = target
            if not self._switch_data_folder(target, get_user_data_dir()):
                QMessageBox.information(self, "Ubicación de datos", f"Los datos ya se guardan en:\n{target}")
        except Exception as exc:
            logger.exception("No se pudo cambiar la carpeta de datos.")
            QMessageBox.critical(self, "Error al migrar datos", str(exc))

    def _switch_data_folder(self, target: str, current_dir: Path) -> bool:
        """Move the app to another data folder. Returns False if it was already in use.

        The live session is bound to the old database file, so after the
        switch the database is re-initialised (integrity check + schema
        migrations) and the user is logged out; logging in again opens a
        session on the new file and nothing more is written to the old one.
        """
        new_dir = self._settings_manager.apply_data_folder(target, current_dir=current_dir)
        if new_dir.resolve() == current_dir.resolve():
            return False
        self._session.commit()
        init_db()
        QMessageBox.information(
            self,
            "Ubicación de datos actualizada",
            "FAMILYFIN usará a partir de ahora la base de datos de:\n"
            f"{new_dir}\n\nVuelve a iniciar sesión para continuar.",
        )
        parent = self.parentWidget()
        while parent is not None and not hasattr(parent, "_on_logout"):
            parent = parent.parentWidget()
        if parent is not None:
            parent._on_logout()
        return True

    def _create_backup(self) -> None:
        self._collect_from_form()
        try:
            self._settings_manager.ensure_dirs(self._settings)
            target_dir = self._settings.backup_folder or self._settings.export_folder or "."
            backup_path = self._settings_manager.backup_all_data(target_dir, user_name=self._user.username)
            QMessageBox.information(self, "Copia de seguridad", f"Se creó la copia de seguridad en:\n{backup_path}")
        except Exception as exc:
            QMessageBox.critical(self, "Error al crear la copia de seguridad", str(exc))

    def _restore_backup(self) -> None:
        backup_path, _ = QFileDialog.getOpenFileName(
            self,
            "Selecciona un respaldo de FAMILYFIN",
            self._settings.backup_folder or ".",
            "Archivo ZIP (*.zip)",
        )
        if not backup_path:
            return
        try:
            ok = self._settings_manager.restore_all_data(backup_path)
            if ok:
                QMessageBox.information(
                    self,
                    "Restauración preparada",
                    "La copia de seguridad es válida y está preparada.\n\n"
                    "Cierra y vuelve a abrir FAMILYFIN para completar la restauración. "
                    "Los datos actuales se guardarán antes en una copia (profin.before-restore.*.db).",
                )
            else:
                QMessageBox.warning(self, "Restauración inválida", "El archivo de backup no es válido para FAMILYFIN.")
        except Exception as exc:
            QMessageBox.critical(self, "Error al restaurar", str(exc))

    def apply_theme(self, theme_key: str) -> None:
        self._theme = theme_key
        self._t = THEMES[theme_key]
        self._apply_theme()

    def _apply_theme(self) -> None:
        t = self._t
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {t["bg_primary"]};
                color: {t["text_primary"]};
                font-family: "Segoe UI";
            }}
            QFrame#dashTopBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLabel#dashTitle {{
                color: {t["text_primary"]};
                font-size: 15px; font-weight: 700;
            }}
            QFrame#settingsSidebar {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 10px;
            }}
            QPushButton#settingsNavButton {{
                text-align: left;
                padding-left: 12px;
                border-radius: 8px;
                background-color: transparent;
                color: {t["text_secondary"]};
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton#settingsNavButton:checked {{
                background-color: {t["accent_light"]};
                color: {t["accent"]};
            }}
            QPushButton#btnPrimary {{
                background-color: {t["accent"]};
                color: #FFFFFF;
                border: none;
                border-radius: 8px;
                padding: 0 16px;
                font-weight: 600;
            }}
            QGroupBox {{
                border: 1px solid {t["border"]};
                border-radius: 10px;
                margin-top: 12px;
                font-size: 12px;
                font-weight: 600;
                color: {t["text_primary"]};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }}
            QComboBox, QLineEdit, QSpinBox {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 7px;
                min-height: 32px;
                padding: 0 8px;
                color: {t["text_primary"]};
            }}
            QCheckBox {{
                spacing: 8px;
                color: {t["text_primary"]};
            }}
            QLabel {{
                color: {t["text_primary"]};
            }}
        """)
        apply_card_shadow(self._nav_panel, t)
