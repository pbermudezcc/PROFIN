"""
app/gui/categories_view.py
===========================
FAMILYFIN — Categories & Subcategories View (Fase 4b)

Features:
  - Tree view: Type → Category hierarchy
  - Add / Rename / Delete categories
  - Seed default categories button (first-time setup)
  - Export / import the categorization library (.profin-cats.json)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QFont, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QTreeView, QHeaderView,
    QInputDialog, QMessageBox, QSizePolicy, QFileDialog, QComboBox,
)
import qtawesome as qta

from sqlalchemy.orm import Session

from app.config      import THEMES
from app.db.models   import (
    CATEGORY_NATURE_FIXED, CATEGORY_NATURE_TRANSFER,
    AutoCategorizationRule, Category, CategoryLevel, User,
)
from app.gui._style_utils import apply_card_shadow
from app.categorization.category_manager import get_all_categories
from app.categorization.rules_engine import shares_global_rules
from app.categorization import library_io
from app.categorization.admin_service import (
    NATURE_LABELS, add_category, apply_all_rules, load_defaults, remove_category,
    rename_category, set_category_nature, transfer_peers,
)
from app.gui.library_dialog import LibraryImportDialog

logger = logging.getLogger(__name__)


class CategoriesView(QWidget):

    def __init__(self, user: User, session: Session,
                 theme: str = "light") -> None:
        super().__init__()
        self._user    = user
        self._session = session
        self._theme   = theme
        self._t       = THEMES[theme]

        self._build_ui()
        self._apply_theme()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_topbar())

        # Body — tree + action panel side by side
        body = QHBoxLayout()
        body.setContentsMargins(24, 16, 24, 16)
        body.setSpacing(16)

        # Tree view
        self._tree = QTreeView()
        self._tree.setHeaderHidden(True)
        self._tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._tree.setAlternatingRowColors(True)
        self._tree.setAnimated(True)
        self._tree.setObjectName("catTree")
        self._tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._tree_model = QStandardItemModel()
        self._tree.setModel(self._tree_model)
        self._tree.selectionModel().currentChanged.connect(
            self._on_selection_changed
        )
        body.addWidget(self._tree, stretch=2)

        # Action panel
        self._action_panel = self._make_action_panel()
        body.addWidget(self._action_panel, stretch=1)

        body_widget = QWidget()
        body_widget.setLayout(body)
        root.addWidget(body_widget)

    def _make_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("dashTopBar")
        bar.setFixedHeight(56)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)

        lbl = QLabel("Categorías")
        lbl.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        lbl.setObjectName("dashTitle")
        layout.addWidget(lbl)

        layout.addStretch()

        btn_seed = QPushButton("  Cargar categorías por defecto")
        btn_seed.setIcon(
            qta.icon("mdi.database-import-outline", color="#FFFFFF")
        )
        btn_seed.setFixedHeight(34)
        btn_seed.setObjectName("btnPrimary")
        btn_seed.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_seed.clicked.connect(self._on_seed)

        for text, icon, slot in (
            ("  Exportar…", "mdi.export", self._on_export),
            ("  Importar…", "mdi.import", self._on_import),
        ):
            btn = QPushButton(text)
            btn.setIcon(qta.icon(icon, color=self._t["text_secondary"]))
            btn.setFixedHeight(34)
            btn.setObjectName("btnSecondary")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("Guarda o carga tus categorías y reglas en un archivo "
                           f"{library_io.FILE_SUFFIX}")
            btn.clicked.connect(slot)
            layout.addWidget(btn)
        layout.addWidget(btn_seed)

        return bar

    def _make_action_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("actionPanel")
        panel.setMinimumWidth(220)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        lbl = QLabel("Acciones")
        lbl.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        lbl.setObjectName("panelTitle")
        layout.addWidget(lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("panelSep")
        layout.addWidget(sep)

        # Selected info
        self._lbl_selected = QLabel("Selecciona una categoría")
        self._lbl_selected.setObjectName("selectedInfo")
        self._lbl_selected.setWordWrap(True)
        layout.addWidget(self._lbl_selected)

        layout.addSpacing(8)

        # Add subcategory
        self._btn_add_sub = QPushButton("  Añadir subcategoría")
        self._btn_add_sub.setIcon(
            qta.icon("mdi.plus", color="#FFFFFF")
        )
        self._btn_add_sub.setFixedHeight(38)
        self._btn_add_sub.setObjectName("btnPrimary")
        self._btn_add_sub.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_sub.setEnabled(False)
        self._btn_add_sub.clicked.connect(self._on_add_sub)
        layout.addWidget(self._btn_add_sub)

        # Rename
        self._btn_rename = QPushButton("  Renombrar")
        self._btn_rename.setIcon(
            qta.icon("mdi.pencil-outline",
                     color=self._t["text_secondary"])
        )
        self._btn_rename.setFixedHeight(38)
        self._btn_rename.setObjectName("btnSecondary")
        self._btn_rename.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_rename.setEnabled(False)
        self._btn_rename.clicked.connect(self._on_rename)
        layout.addWidget(self._btn_rename)

        # Delete
        self._btn_delete = QPushButton("  Eliminar")
        self._btn_delete.setIcon(
            qta.icon("mdi.delete-outline", color="#FFFFFF")
        )
        self._btn_delete.setFixedHeight(38)
        self._btn_delete.setObjectName("btnDanger")
        self._btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_delete.setEnabled(False)
        self._btn_delete.clicked.connect(self._on_delete)
        layout.addWidget(self._btn_delete)

        layout.addSpacing(8)

        # Fijo / variable / traspaso (saved on change, like the web sheet)
        lbl_nature = QLabel("Tipo de gasto")
        lbl_nature.setObjectName("selectedInfo")
        layout.addWidget(lbl_nature)
        self._cmb_nature = QComboBox()
        self._cmb_nature.setFixedHeight(34)
        for value, label in NATURE_LABELS.items():
            self._cmb_nature.addItem(label, value)
        self._cmb_nature.setToolTip(
            "Fijo: recibos que se repiten (hipoteca, luz, seguros, nómina); cuentan en Previsiones.\n"
            "Traspaso interno: dinero entre miembros de la familia (aportes); no es gasto ni ingreso.")
        self._cmb_nature.setEnabled(False)
        self._cmb_nature.currentIndexChanged.connect(self._on_nature_changed)
        layout.addWidget(self._cmb_nature)

        self._lbl_peer = QLabel("Con (miembro que envía o recibe)")
        self._lbl_peer.setObjectName("selectedInfo")
        self._lbl_peer.setWordWrap(True)
        layout.addWidget(self._lbl_peer)
        self._cmb_peer = QComboBox()
        self._cmb_peer.setFixedHeight(34)
        self._cmb_peer.currentIndexChanged.connect(self._on_nature_changed)
        layout.addWidget(self._cmb_peer)
        self._lbl_peer.setVisible(False)
        self._cmb_peer.setVisible(False)

        layout.addStretch()

        return panel

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._tree_model.clear()
        self._cat_id_map: dict = {}  # item -> category_id

        types = get_all_categories(
            self._session, self._user.id, level=CategoryLevel.type
        )
        cats  = get_all_categories(
            self._session, self._user.id, level=CategoryLevel.category
        )

        # Group categories by parent_id
        by_parent: dict = {}
        for cat in cats:
            by_parent.setdefault(cat.parent_id, []).append(cat)

        type_icons = {
            "GASTO":          ("mdi.trending-down",  self._t["expense"]),
            "INGRESO":        ("mdi.trending-up",    self._t["income"]),
            "INVERSION":      ("mdi.chart-line",     self._t["investment"]),
            "SERV. BANCARIO": ("mdi.bank-outline",   self._t["accent"]),
        }

        for type_node in types:
            icon_name, color = type_icons.get(
                type_node.name,
                ("mdi.tag-outline", self._t["accent"])
            )
            type_item = QStandardItem(
                qta.icon(icon_name, color=color), type_node.name
            )
            type_item.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
            type_item.setData(type_node.id, Qt.ItemDataRole.UserRole)
            type_item.setData("type", Qt.ItemDataRole.UserRole + 1)
            self._tree_model.appendRow(type_item)

            for cat in by_parent.get(type_node.id, []):
                cat_item = QStandardItem(cat.name)
                cat_item.setData(cat.id, Qt.ItemDataRole.UserRole)
                cat_item.setData("category", Qt.ItemDataRole.UserRole + 1)
                self._show_nature(cat_item, cat.nature)
                type_item.appendRow(cat_item)

        self._tree.collapseAll()
        self._btn_add_sub.setEnabled(False)
        self._btn_rename.setEnabled(False)
        self._btn_delete.setEnabled(False)
        self._load_nature(None)

    def _show_nature(self, item: QStandardItem, nature: Optional[str]) -> None:
        """Icon + tooltip for a category's nature (the text stays its name)."""
        icon, color = {
            CATEGORY_NATURE_FIXED:    ("mdi.pin", self._t["accent"]),
            CATEGORY_NATURE_TRANSFER: ("mdi.swap-horizontal", self._t["investment"]),
        }.get(nature, ("mdi.tag-outline", self._t["text_secondary"]))
        item.setIcon(qta.icon(icon, color=color))
        item.setToolTip(NATURE_LABELS.get(nature, "Variable"))

    def _load_nature(self, cat: Optional[Category]) -> None:
        """Fill the Tipo de gasto / Con pickers without saving anything."""
        self._loading_nature = True
        try:
            self._cmb_nature.setEnabled(cat is not None)
            self._cmb_nature.setCurrentIndex(
                max(0, self._cmb_nature.findData(cat.nature if cat else None)))
            peers = transfer_peers(self._session, self._user.id)
            self._cmb_peer.clear()
            self._cmb_peer.addItem("Sin indicar", None)
            for peer in peers:
                self._cmb_peer.addItem(peer.username, peer.id)
            show_peer = bool(cat is not None and cat.nature == CATEGORY_NATURE_TRANSFER and peers)
            self._lbl_peer.setVisible(show_peer)
            self._cmb_peer.setVisible(show_peer)
            if show_peer:
                self._cmb_peer.setCurrentIndex(max(0, self._cmb_peer.findData(cat.transfer_peer_user_id)))
        finally:
            self._loading_nature = False

    def _on_nature_changed(self, _index: int) -> None:
        if getattr(self, "_loading_nature", False):
            return
        item = self._tree_model.itemFromIndex(self._tree.currentIndex())
        if item is None or item.data(Qt.ItemDataRole.UserRole + 1) != "category":
            return
        nature = self._cmb_nature.currentData()
        peer = self._cmb_peer.currentData() if nature == CATEGORY_NATURE_TRANSFER else None
        try:
            cat = set_category_nature(self._session, self._user.id,
                                      item.data(Qt.ItemDataRole.UserRole), nature, peer)
        except ValueError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return
        self._show_nature(item, cat.nature)
        self._load_nature(cat)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_selection_changed(self, current, previous) -> None:
        item = self._tree_model.itemFromIndex(current)
        if item is None:
            return

        level    = item.data(Qt.ItemDataRole.UserRole + 1)
        cat_name = item.text()

        self._lbl_selected.setText(
            f"<b>{cat_name}</b><br>"
            f"<small>Nivel: {level or '—'}</small>"
        )

        is_type     = level == "type"
        is_category = level == "category"

        self._btn_add_sub.setEnabled(is_type)
        self._btn_rename.setEnabled(is_type or is_category)
        self._btn_delete.setEnabled(is_category)
        self._load_nature(
            self._session.get(Category, item.data(Qt.ItemDataRole.UserRole)) if is_category else None)

    def _on_add_sub(self) -> None:
        idx  = self._tree.currentIndex()
        item = self._tree_model.itemFromIndex(idx)
        if item is None:
            return
        parent_id = item.data(Qt.ItemDataRole.UserRole)

        name, ok = QInputDialog.getText(
            self, "Nueva categoría",
            "Nombre de la nueva categoría:"
        )
        if not ok or not name.strip():
            return
        try:
            add_category(self._session, self._user.id, parent_id, name.strip())
            self.refresh()
        except ValueError as exc:
            QMessageBox.warning(self, "Error", str(exc))

    def _on_rename(self) -> None:
        idx  = self._tree.currentIndex()
        item = self._tree_model.itemFromIndex(idx)
        if item is None:
            return
        cat_id   = item.data(Qt.ItemDataRole.UserRole)
        old_name = item.text()

        name, ok = QInputDialog.getText(
            self, "Renombrar",
            "Nuevo nombre:", text=old_name
        )
        if not ok or not name.strip():
            return
        try:
            rename_category(self._session, self._user.id, cat_id, name.strip())
            self.refresh()
        except ValueError as exc:
            QMessageBox.warning(self, "Error", str(exc))

    def _on_delete(self) -> None:
        idx  = self._tree.currentIndex()
        item = self._tree_model.itemFromIndex(idx)
        if item is None:
            return
        cat_id   = item.data(Qt.ItemDataRole.UserRole)
        cat_name = item.text()

        reply = QMessageBox.question(
            self, "Confirmar eliminación",
            f"¿Eliminar la categoría <b>{cat_name}</b>?<br>"
            "Las transacciones asignadas quedarán sin categorizar.",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            remove_category(self._session, self._user.id, cat_id)
            self.refresh()
        except ValueError as exc:
            QMessageBox.warning(self, "Error", str(exc))

    def _on_seed(self) -> None:
        reply = QMessageBox.question(
            self, "Cargar categorías por defecto",
            "Se añadirán las categorías estándar de FAMILYFIN "
            "(solo si aún no existen).<br>¿Continuar?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        created, rules_created = load_defaults(self._session, self._user.id)
        self.refresh()
        if created > 0 or rules_created > 0:
            QMessageBox.information(
                self, "Categorías cargadas",
                f"Se crearon {created} categorías y {rules_created} reglas "
                "por defecto."
            )
        else:
            QMessageBox.information(
                self, "Sin cambios",
                "Las categorías y reglas por defecto ya estaban creadas."
            )

    # ------------------------------------------------------------------
    # Export / import of the categorization library
    # ------------------------------------------------------------------

    def _on_export(self) -> None:
        include_global = False
        if shares_global_rules(self._session, self._user.id) and self._session.query(
                AutoCategorizationRule.id).filter(AutoCategorizationRule.user_id.is_(None)).first() is not None:
            reply = QMessageBox.question(
                self, "Exportar categorías y reglas",
                "¿Incluir también las reglas globales (compartidas por toda la familia)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            include_global = reply == QMessageBox.StandardButton.Yes
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar categorías y reglas",
            library_io.suggested_filename(self._user.username),
            f"Biblioteca de categorías (*{library_io.FILE_SUFFIX})",
        )
        if not path:
            return
        payload = library_io.export_library(self._session, self._user.id, include_global,
                                            source_user=self._user.username)
        try:
            Path(path).write_bytes(library_io.dumps(payload))
        except OSError as exc:
            QMessageBox.warning(self, "Error", f"No se pudo guardar el archivo:\n{exc}")
            return
        categories = sum(len(t["categories"]) for t in payload["types"])
        QMessageBox.information(
            self, "Exportación completada",
            f"Se exportaron {categories} categorías y {len(payload['rules'])} reglas a:\n{path}",
        )

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Importar categorías y reglas", "",
            f"Biblioteca de categorías (*{library_io.FILE_SUFFIX} *.json *.json.gz);;Todos (*)",
        )
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                raw = handle.read(library_io.MAX_FILE_BYTES + 1)
            lib = library_io.parse_library(raw)
        except (OSError, library_io.LibraryError) as exc:
            QMessageBox.warning(self, "No se puede importar", str(exc))
            return

        dialog = LibraryImportDialog(self._session, self._user.id, lib, Path(path).name, self)
        if dialog.exec() != LibraryImportDialog.DialogCode.Accepted:
            return
        mode, allow_global = dialog.mode, dialog.allow_global
        if mode == library_io.MODE_REPLACE and dialog.plan and dialog.plan.deleted_rules:
            reply = QMessageBox.warning(
                self, "Confirmar reemplazo",
                f"Se eliminarán {len(dialog.plan.deleted_rules)} reglas tuyas que no están "
                "en el archivo. ¿Continuar?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        safety = self._save_pre_import_copy()
        try:
            result = library_io.apply_import(self._session, self._user.id, lib, mode, allow_global)
        except Exception as exc:  # apply_import already rolled back
            logger.exception("Importación de categorías fallida")
            QMessageBox.warning(self, "Error", f"No se importó nada: {exc}")
            return
        self.refresh()

        lines = [
            f"Categorías creadas: {result.types_created + result.categories_created}",
            f"Reglas creadas: {result.rules_created}",
        ]
        if mode == library_io.MODE_REPLACE:
            lines += [f"Prioridades actualizadas: {result.rules_updated}",
                      f"Reglas eliminadas: {result.rules_deleted}"]
        if safety:
            lines.append(f"\nCopia previa guardada en:\n{safety}")
        lines.append("\n¿Aplicar ahora las reglas a los movimientos sin categoría?")
        reply = QMessageBox.question(
            self, "Importación completada", "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            summary = apply_all_rules(self._session, self._user.id, overwrite=False)
            QMessageBox.information(
                self, "Reglas aplicadas",
                f"Movimientos categorizados: {summary.assigned}\n"
                f"Con conflicto: {summary.conflicts}\nSin regla: {summary.unmatched}",
            )

    def _save_pre_import_copy(self) -> Optional[Path]:
        """Export the current library next to the backups before changing it."""
        try:
            from app.settings.settings_manager import SettingsManager
            manager = SettingsManager()
            folder = manager.auto_backup_dir(manager.load())
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = library_io.suggested_filename(self._user.username).replace(
                library_io.FILE_SUFFIX, f"-antes-de-importar-{stamp}{library_io.FILE_SUFFIX}")
            payload = library_io.export_library(self._session, self._user.id, include_global=True,
                                                source_user=self._user.username)
            (folder / name).write_bytes(library_io.dumps(payload))
            return folder / name
        except Exception:  # a failed safety copy must not block the import
            logger.exception("No se pudo guardar la copia previa de categorías")
            return None

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def apply_theme(self, theme_key: str) -> None:
        self._theme = theme_key
        self._t = THEMES[theme_key]
        self._apply_theme()

    def _apply_theme(self) -> None:
        t = self._t
        is_dark = self._theme == "dark"
        selection_bg = (
            f"background-color: {t['accent']}; color: #FFFFFF;"
            if is_dark else
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 {t['selection_grad_start']}, stop:1 {t['selection_grad_end']}); "
            f"color: {t['selection_text']};"
        )
        selection_hover = (
            f"background-color: {t['accent_hover']}; color: #FFFFFF;"
            if is_dark else
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 {t['selection_hover_start']}, stop:1 {t['selection_hover_end']}); "
            f"color: {t['selection_text']};"
        )
        self.setStyleSheet(f"""
            QFrame#dashTopBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLabel#dashTitle {{
                color: {t["text_primary"]};
                font-size: 16px; font-weight: 700;
            }}
            QPushButton#btnPrimary {{
                background-color: {t["accent"]}; color: #FFFFFF;
                border: none; border-radius: 8px;
                font-size: 13px; font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton#btnPrimary:hover {{
                background-color: {t["accent_hover"]};
            }}
            QPushButton#btnSecondary {{
                background-color: transparent;
                color: {t["text_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px; font-size: 13px;
                padding: 0 16px;
            }}
            QPushButton#btnSecondary:hover {{
                background-color: {t["bg_hover"]};
            }}
            QPushButton#btnDanger {{
                background-color: {t["expense"]}; color: #FFFFFF;
                border: none; border-radius: 8px;
                font-size: 13px; font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton#btnDanger:disabled {{
                background-color: {t["border"]};
                color: {t["text_secondary"]};
            }}
            QTreeView#catTree {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                color: {t["text_primary"]};
                font-size: 13px;
                alternate-background-color: {t["bg_primary"]};
            }}
            QTreeView#catTree::item {{
                padding: 6px 8px;
                border-radius: 4px;
            }}
            QTreeView#catTree::item:hover {{
                background-color: {t["bg_hover"]};
                color: {t["text_primary"]};
            }}
            QTreeView#catTree::item:selected {{
                {selection_bg}
            }}
            QTreeView#catTree::item:selected:hover {{
                {selection_hover}
            }}
            QFrame#actionPanel {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 12px;
            }}
            QLabel#panelTitle {{
                color: {t["text_primary"]};
                font-size: 13px; font-weight: 700;
            }}
            QLabel#selectedInfo {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QFrame#panelSep {{
                color: {t["border"]};
                background-color: {t["border"]};
                max-height: 1px;
            }}
        """)
        apply_card_shadow(self._action_panel, t)