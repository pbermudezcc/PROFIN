"""
app/gui/rules_view.py
======================
FAMILYFIN — Auto-Categorization Rules View (Fase 4b)

Features:
  - List of all rules (global + user) ordered by priority
  - Create new rule: pattern + match type + category + priority
  - Delete rule
  - Visual indicator for match type (contains/exact/regex)
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QFont, QColor, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QTableView, QHeaderView,
    QDialog, QLineEdit, QComboBox, QSpinBox,
    QFormLayout, QDialogButtonBox, QMessageBox,
    QAbstractItemView, QSizePolicy,
)
import qtawesome as qta

from sqlalchemy.orm import Session

from app.config      import THEMES
from app.db.models   import (
    AutoCategorizationRule, MatchType, User, Category, CategoryLevel,
    Transaction,
)
from app.categorization.rules_engine  import (
    apply_rules, create_rule, delete_rule, get_all_rules, update_rule
)
from app.categorization.category_manager import get_all_categories
from app.gui._style_utils import apply_card_shadow


class RulesView(QWidget):

    def __init__(self, user: User, session: Session,
                 theme: str = "light") -> None:
        super().__init__()
        self._user    = user
        self._session = session
        self._theme   = theme
        self._t       = THEMES[theme]
        self._rules:  List[AutoCategorizationRule] = []
        self._categories: List[Category] = []

        self._build_ui()
        self._apply_theme()
        self._load_categories()
        self.refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_topbar())

        # Table
        self._table = QTableView()
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels([
            "Prioridad", "Patrón", "Tipo match",
            "Categoría", "Ámbito", "Transacciones vinculadas",
            "Total (€)",
        ])
        self._table.setModel(self._model)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)

        root.addWidget(self._table)

        # Bottom bar
        bottom = QFrame()
        self._bottom_bar = bottom
        bottom.setObjectName("rulesBottom")
        bottom.setFixedHeight(52)
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(16, 8, 16, 8)
        bl.setSpacing(10)

        self._lbl_count = QLabel("")
        self._lbl_count.setObjectName("rulesCount")
        bl.addWidget(self._lbl_count)

        bl.addStretch()

        btn_edit = QPushButton("  Editar regla")
        btn_edit.setIcon(
            qta.icon("mdi.pencil-outline", color="#FFFFFF")
        )
        btn_edit.setFixedHeight(36)
        btn_edit.setObjectName("btnSecondary")
        btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_edit.setToolTip("Editar la regla seleccionada")
        btn_edit.clicked.connect(self._on_edit_rule)
        bl.addWidget(btn_edit)

        btn_del = QPushButton("  Eliminar regla")
        btn_del.setIcon(
            qta.icon("mdi.delete-outline", color="#FFFFFF")
        )
        btn_del.setFixedHeight(36)
        btn_del.setObjectName("btnDanger")
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.clicked.connect(self._on_delete)
        bl.addWidget(btn_del)

        btn_apply = QPushButton("  Aplicar reglas a transacciones")
        btn_apply.setIcon(
            qta.icon("mdi.auto-fix", color="#FFFFFF")
        )
        btn_apply.setFixedHeight(36)
        btn_apply.setObjectName("btnAccent")
        btn_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_apply.setToolTip(
            "Aplica todas las reglas activas a las transacciones sin categorizar"
        )
        btn_apply.clicked.connect(self._on_apply_rules)
        bl.addWidget(btn_apply)

        root.addWidget(bottom)

    def _make_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("dashTopBar")
        bar.setFixedHeight(56)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)

        lbl = QLabel("Reglas de auto-categorización")
        lbl.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        lbl.setObjectName("dashTitle")
        layout.addWidget(lbl)

        layout.addStretch()

        btn_new = QPushButton("  Nueva regla")
        btn_new.setIcon(
            qta.icon("mdi.plus", color="#FFFFFF")
        )
        btn_new.setFixedHeight(34)
        btn_new.setObjectName("btnPrimary")
        btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_new.clicked.connect(self._on_new_rule)
        layout.addWidget(btn_new)

        return bar

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_categories(self) -> None:
        self._categories = get_all_categories(
            self._session, self._user.id, level=CategoryLevel.category
        )

    def refresh(self) -> None:
        self._rules = get_all_rules(self._session, user_id=self._user.id)
        self._model.removeRows(0, self._model.rowCount())
        t = self._t
        rule_transactions = self._get_rule_transactions()

        cat_map = {c.id: c.name for c in self._categories}
        match_labels = {
            MatchType.contains: "Contiene",
            MatchType.exact:    "Exacto",
            MatchType.regex:    "Regex",
        }
        match_colors = {
            MatchType.contains: t["accent"],
            MatchType.exact:    t["income"],
            MatchType.regex:    t["investment"],
        }

        for rule in self._rules:
            item_pri = QStandardItem(str(rule.priority))
            item_pri.setData(rule.id, Qt.ItemDataRole.UserRole)
            item_pri.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_pat = QStandardItem(rule.match_pattern)

            item_mt = QStandardItem(
                match_labels.get(rule.match_type, str(rule.match_type))
            )
            item_mt.setForeground(
                QColor(match_colors.get(rule.match_type, t["text_primary"]))
            )
            item_mt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_mt.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))

            cat_name = cat_map.get(rule.category_id, f"id={rule.category_id}")
            item_cat = QStandardItem(cat_name)

            scope = "Global" if rule.user_id is None else "Personal"
            item_scope = QStandardItem(scope)
            item_scope.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if scope == "Global":
                item_scope.setForeground(QColor(t["investment"]))
            else:
                item_scope.setForeground(QColor(t["text_secondary"]))

            linked_transactions = rule_transactions.get(rule.id, [])
            item_linked = QStandardItem(str(len(linked_transactions)))
            item_linked.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            total_amount = sum(
                transaction.amount for transaction in linked_transactions
            )
            item_total = QStandardItem(_format_amount(total_amount))
            item_total.setTextAlignment(Qt.AlignmentFlag.AlignRight)

            row = [
                item_pri, item_pat, item_mt, item_cat, item_scope,
                item_linked, item_total,
            ]
            for item in row:
                item.setEditable(False)
            self._model.appendRow(row)

        self._lbl_count.setText(
            f"{len(self._rules)} regla(s) activa(s)"
        )

    def _get_rule_transactions(
        self,
    ) -> dict[int, List[Transaction]]:
        """Return transactions assigned to each rule using current priorities."""
        transaction_ids = [
            row.id for row in self._session.query(Transaction.id).filter(
                Transaction.user_id == self._user.id
            ).all()
        ]
        if not transaction_ids:
            return {}

        result = apply_rules(
            self._session, transaction_ids, self._user.id
        )
        transactions = self._session.query(Transaction).filter(
            Transaction.id.in_(result.assigned)
        ).all()
        transactions_by_id = {transaction.id: transaction for transaction in transactions}
        return {
            rule_id: [
                transactions_by_id[transaction_id]
                for transaction_id in assigned_ids
                if transaction_id in transactions_by_id
            ]
            for rule_id, assigned_ids in result.transaction_ids_by_rule.items()
        }

    def _selected_rule(self) -> AutoCategorizationRule | None:
        """Return the rule represented by the currently selected table row."""
        idx = self._table.currentIndex()
        if not idx.isValid():
            return None

        rule_id = self._model.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((rule for rule in self._rules if rule.id == rule_id), None)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_new_rule(self) -> None:
        # Always reload categories before opening the dialog
        # in case they were added in the Categories view after this view loaded
        self._load_categories()

        if not self._categories:
            QMessageBox.warning(
                self, "Sin categorías",
                "No hay categorías disponibles.\n\n"
                "Ve a la pantalla 'Categorías' → pulsa 'Cargar categorías "
                "por defecto' y vuelve aquí."
            )
            return

        t = self._t

        dlg = QDialog(self)
        dlg.setWindowTitle("Nueva regla de auto-categorización")
        dlg.setFixedWidth(440)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)

        # Pattern
        inp_pattern = QLineEdit()
        inp_pattern.setPlaceholderText("ej: MERCADONA, ^REPSOL, NOMINA PEDRO")
        inp_pattern.setFixedHeight(38)
        form.addRow("Patrón:", inp_pattern)

        # Match type
        cmb_type = QComboBox()
        cmb_type.setFixedHeight(38)
        cmb_type.addItem("Contiene (substring)", MatchType.contains)
        cmb_type.addItem("Exacto (full match)",  MatchType.exact)
        cmb_type.addItem("Regex (expresión regular)", MatchType.regex)
        form.addRow("Tipo de match:", cmb_type)

        # Category
        cmb_cat = QComboBox()
        cmb_cat.setFixedHeight(38)
        for cat in self._categories:
            cmb_cat.addItem(cat.name, cat.id)
        form.addRow("Categoría:", cmb_cat)

        # Priority
        spn_pri = QSpinBox()
        spn_pri.setRange(1, 999)
        spn_pri.setValue(100)
        spn_pri.setFixedHeight(38)
        form.addRow("Prioridad (1=mayor):", spn_pri)

        # Scope
        cmb_scope = QComboBox()
        cmb_scope.setFixedHeight(38)
        cmb_scope.addItem("Global (todos los usuarios)", None)
        cmb_scope.addItem("Personal (solo yo)",         self._user.id)
        form.addRow("Ámbito:", cmb_scope)

        layout.addLayout(form)

        # Hint
        lbl_hint = QLabel(
            "💡 Tip: usa <b>Contiene</b> para la mayoría de casos. "
            "Usa <b>Regex</b> para patrones avanzados."
        )
        lbl_hint.setWordWrap(True)
        lbl_hint.setStyleSheet(
            f"color: {t['text_secondary']}; font-size: 11px;"
        )
        layout.addWidget(lbl_hint)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("Crear regla")
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setObjectName("btnPrimary")
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addWidget(btn_box)

        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {t["bg_secondary"]}; }}
            QLineEdit, QComboBox, QSpinBox {{
                background-color: {t["bg_primary"]};
                border: 1.5px solid {t["border"]};
                border-radius: 6px;
                padding: 4px 10px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
                border-color: {t["accent"]};
            }}
            QLabel {{
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QPushButton#btnPrimary {{
                background-color: {t["accent"]}; color: #FFFFFF;
                border: none; border-radius: 6px;
                font-size: 13px; font-weight: 600;
                padding: 6px 16px;
            }}
        """)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pattern  = inp_pattern.text().strip()
        mt       = cmb_type.currentData()
        cat_id   = cmb_cat.currentData()
        priority = spn_pri.value()
        scope    = cmb_scope.currentData()

        if not pattern:
            QMessageBox.warning(
                self, "Patrón vacío",
                "El patrón no puede estar vacío."
            )
            return

        if cat_id is None:
            QMessageBox.warning(
                self, "Sin categoría",
                "Selecciona una categoría válida para la regla.\n\n"
                "Si no hay categorías disponibles, ve a la pantalla "
                "'Categorías' y carga las categorías por defecto primero."
            )
            return

        try:
            create_rule(
                self._session,
                category_id   = cat_id,
                match_pattern = pattern,
                match_type    = mt,
                priority      = priority,
                user_id       = scope,
            )
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "Error al crear regla", str(exc))

    def _on_apply_rules(self) -> None:
        """
        Apply all active rules to every uncategorized transaction
        belonging to this user and refresh the rules count label.
        """
        import logging
        _log = logging.getLogger(__name__)

        try:
            from app.categorization.admin_service import apply_all_rules

            # Re-process ALL transactions — overwrite=True ensures that
            # transactions already categorized by a previous version of the
            # rule are updated with the new category. Manually categorized
            # ones (category_source='manual') are always kept.
            result = apply_all_rules(self._session, self._user.id, overwrite=True)
            if not (result.assigned or result.conflicts or result.unmatched):
                QMessageBox.information(
                    self,
                    "Sin transacciones",
                    "No hay transacciones en la base de datos.",
                )
                return

            n_assigned  = result.assigned
            n_conflict  = result.conflicts
            n_unmatched = result.unmatched

            _log.info(
                "Reglas aplicadas: %d asignadas, %d conflictos, %d sin match",
                n_assigned, n_conflict, n_unmatched,
            )

            msg = (
                f"<b>{n_assigned}</b> transacciones categorizadas automáticamente."
            )
            if n_conflict:
                msg += (
                    f"<br><b>{n_conflict}</b> transacciones con conflicto de reglas "
                    f"(múltiples reglas coinciden) — requieren revisión manual."
                )
            if n_unmatched:
                msg += (
                    f"<br><b>{n_unmatched}</b> transacciones sin coincidencia "
                    f"— permanecen sin categorizar."
                )

            QMessageBox.information(self, "Reglas aplicadas", msg)
            self.refresh()

        except Exception as exc:
            QMessageBox.critical(
                self, "Error al aplicar reglas", str(exc)
            )

    def _on_edit_rule(self) -> None:
        """Open a pre-filled dialog to edit the selected rule."""
        rule = self._selected_rule()
        if rule is None:
            QMessageBox.information(
                self, "Sin selección",
                "Selecciona una regla para editar."
            )
            return

        self._load_categories()

        if not self._categories:
            QMessageBox.warning(
                self, "Sin categorías",
                "No hay categorías disponibles para asignar."
            )
            return

        t = self._t

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Editar regla — '{rule.match_pattern}'")
        dlg.setFixedWidth(440)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)

        # Pattern — pre-filled
        inp_pattern = QLineEdit(rule.match_pattern)
        inp_pattern.setFixedHeight(38)
        form.addRow("Patrón:", inp_pattern)

        # Match type — pre-selected
        cmb_type = QComboBox()
        cmb_type.setFixedHeight(38)
        cmb_type.addItem("Contiene (substring)",      MatchType.contains)
        cmb_type.addItem("Exacto (full match)",        MatchType.exact)
        cmb_type.addItem("Regex (expresión regular)",  MatchType.regex)
        cmb_type.setCurrentIndex(
            [MatchType.contains, MatchType.exact, MatchType.regex]
            .index(rule.match_type)
        )
        form.addRow("Tipo de match:", cmb_type)

        # Category — pre-selected
        cmb_cat = QComboBox()
        cmb_cat.setFixedHeight(38)
        for cat in self._categories:
            cmb_cat.addItem(cat.name, cat.id)
        # Select current category
        for i in range(cmb_cat.count()):
            if cmb_cat.itemData(i) == rule.category_id:
                cmb_cat.setCurrentIndex(i)
                break
        form.addRow("Categoría:", cmb_cat)

        # Priority — pre-filled
        spn_pri = QSpinBox()
        spn_pri.setRange(1, 999)
        spn_pri.setValue(rule.priority)
        spn_pri.setFixedHeight(38)
        form.addRow("Prioridad (1=mayor):", spn_pri)

        # Scope — pre-selected (read-only info, scope cannot change)
        scope_label = "Global" if rule.user_id is None else "Personal"
        lbl_scope = QLabel(scope_label)
        lbl_scope.setFixedHeight(38)
        form.addRow("Ámbito:", lbl_scope)

        layout.addLayout(form)

        # Dialog buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Save).setText("  Guardar cambios")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("  Cancelar")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {t["bg_primary"]}; }}
            QLabel  {{ color: {t["text_primary"]}; font-size: 13px; }}
            QLineEdit, QComboBox, QSpinBox {{
                background-color: {t["bg_secondary"]};
                color: {t["text_primary"]};
                border: 1px solid {t["border"]};
                border-radius: 6px; padding: 0 10px;
            }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
                border-color: {t["accent"]};
            }}
        """)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pattern  = inp_pattern.text().strip()
        mt       = cmb_type.currentData()
        cat_id   = cmb_cat.currentData()
        priority = spn_pri.value()

        if not pattern:
            QMessageBox.warning(self, "Patrón vacío",
                                "El patrón no puede estar vacío.")
            return

        if cat_id is None:
            QMessageBox.warning(self, "Sin categoría",
                                "Selecciona una categoría válida.")
            return

        try:
            update_rule(
                self._session,
                rule_id       = rule.id,
                category_id   = cat_id,
                match_pattern = pattern,
                match_type    = mt,
                priority      = priority,
            )
            self.refresh()
            QMessageBox.information(
                self, "Regla actualizada",
                f"Regla <b>'{pattern}'</b> actualizada correctamente."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error al editar regla", str(exc))

    def _on_delete(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            QMessageBox.information(
                self, "Sin selección",
                "Selecciona una regla para eliminar."
            )
            return

        reply = QMessageBox.question(
            self,
            "Confirmar eliminación",
            f"¿Eliminar la regla <b>'{rule.match_pattern}'</b>?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_rule(self._session, rule.id)
            self.refresh()
        except ValueError as exc:
            QMessageBox.warning(self, "Error", str(exc))

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
            QPushButton#btnSecondary {{
                background-color: transparent;
                color: {t["text_primary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                font-size: 13px; font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton#btnSecondary:hover {{
                background-color: {t["bg_hover"]};
            }}
            QPushButton#btnAccent {{
                background-color: {t["income"]}; color: #FFFFFF;
                border: none; border-radius: 8px;
                font-size: 13px; font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton#btnAccent:hover {{
                background-color: {t["income_hover"]};
            }}
            QTableView {{
                background-color: {t["bg_secondary"]};
                border: none; color: {t["text_primary"]};
                font-size: 12px;
                alternate-background-color: {t["bg_primary"]};
            }}
            QTableView::item {{
                padding: 8px;
                border-bottom: 1px solid {t["border"]};
            }}
            QTableView::item:hover {{
                background-color: {t["bg_hover"]};
                color: {t["text_primary"]};
            }}
            QTableView::item:selected {{
                {selection_bg}
            }}
            QTableView::item:selected:hover {{
                {selection_hover}
            }}
            QHeaderView::section {{
                background-color: {t["bg_primary"]};
                color: {t["text_secondary"]};
                font-size: 11px; font-weight: 700;
                padding: 6px 8px; border: none;
                border-bottom: 2px solid {t["border"]};
                text-transform: uppercase;
            }}
            QFrame#rulesBottom {{
                background-color: {t["bg_secondary"]};
                border-top: 1px solid {t["border"]};
            }}
            QLabel#rulesCount {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
        """)
        apply_card_shadow(self._bottom_bar, t, blur_radius=12, y_offset=-2)


def _format_amount(value: float) -> str:
    """Format signed amounts with Spanish separators and euro suffix."""
    return (
        f"{value:,.2f} €"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )