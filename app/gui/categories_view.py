"""
app/gui/categories_view.py
===========================
FAMILYFIN — Categories & Subcategories View (Fase 4b)

Features:
  - Tree view: Type → Category hierarchy
  - Add / Rename / Delete categories
  - Seed default categories button (first-time setup)
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QFont, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QTreeView, QHeaderView,
    QInputDialog, QMessageBox, QSizePolicy,
)
import qtawesome as qta

from sqlalchemy.orm import Session

from app.config      import THEMES
from app.db.models   import Category, CategoryLevel, User
from app.categorization.category_manager import (
    get_all_categories, create_category,
    update_category_name, delete_category,
    seed_default_categories,
    seed_default_rules,
)


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
                cat_item = QStandardItem(
                    qta.icon("mdi.tag-outline",
                             color=self._t["text_secondary"]),
                    cat.name
                )
                cat_item.setData(cat.id, Qt.ItemDataRole.UserRole)
                cat_item.setData("category", Qt.ItemDataRole.UserRole + 1)
                type_item.appendRow(cat_item)

        self._tree.collapseAll()
        self._btn_add_sub.setEnabled(False)
        self._btn_rename.setEnabled(False)
        self._btn_delete.setEnabled(False)

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
            create_category(
                self._session, self._user.id,
                name.strip(), CategoryLevel.category,
                parent_id=parent_id,
            )
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
            update_category_name(self._session, cat_id, name.strip())
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
            delete_category(self._session, cat_id)
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

        created = seed_default_categories(
            self._session, self._user.id
        )
        rules_created = seed_default_rules(
            self._session, self._user.id
        )
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
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #DDEBFF, stop:1 #CFE1FF); color: #1E3A8A;"
        )
        selection_hover = (
            f"background-color: {t['accent_hover']}; color: #FFFFFF;"
            if is_dark else
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #CFE1FF, stop:1 #BFD8FF); color: #1E3A8A;"
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