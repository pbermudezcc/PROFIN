"""
app/gui/rules_view.py
======================
FAMILYFIN — Auto-Categorization Rules View (Fase 4b)

Features:
  - List of all rules (global + user) ordered by priority
  - Create new rule: pattern + match type + movement type (gasto /
    ingreso / cualquiera) + category + priority
  - Delete rule
  - Visual indicator for match type (contains/exact/regex)
  - Search box + filters (categoría, tipo, coincidencia, ámbito, uso) over
    the rows built by app.categorization.rule_search; the table only holds
    the filtered rows and every action resolves the rule by the id stored in
    column 0 (never by row index), so sorting/filtering can't misdirect it.
"""
from __future__ import annotations

import logging
from typing import List

from PySide6.QtCore    import Qt, QTimer
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
    Transaction, TransactionType,
)
from app.categorization.rules_engine  import (
    apply_rules, create_rule, delete_rule, get_all_rules, shares_global_rules, update_rule
)
from app.categorization.category_manager import get_all_categories
from app.categorization.rule_search import (
    MATCH_TYPE_LABELS, MOVEMENT_FILTERS, SCOPE_GLOBAL, SCOPE_PERSONAL, USAGE_WITH, USAGE_WITHOUT,
    RuleFilters, RuleRow, build_rule_rows, count_label, filter_options,
    filter_rule_rows,
)
from app.analytics.transaction_search import fold_text
from app.gui._style_utils import apply_card_shadow

logger = logging.getLogger(__name__)

RULE_ID_ROLE = Qt.ItemDataRole.UserRole          # column 0: the rule id
SORT_ROLE    = Qt.ItemDataRole.UserRole + 1      # typed sort key per cell
SEARCH_DEBOUNCE_MS = 250


def _movement_combo(current: TransactionType | None = None) -> QComboBox:
    """Which movements a rule applies to (stored in transaction_type)."""
    cmb = QComboBox()
    cmb.setFixedHeight(38)
    cmb.addItem("Cualquiera (gastos e ingresos)", None)
    cmb.addItem("Solo gastos (cargos)", TransactionType.expense)
    cmb.addItem("Solo ingresos (abonos)", TransactionType.income)
    cmb.setToolTip(
        "Un mismo texto (p. ej. BIZUM) puede ser un pago o un cobro:\n"
        "limita la regla a un tipo para categorizar cada uno por separado."
    )
    cmb.setCurrentIndex(max(0, cmb.findData(current)))
    return cmb


def _category_label(cat: Category) -> str:
    """Name plus its type: the same name can exist under GASTO and INGRESO."""
    return f"{cat.name} ({cat.parent.name})" if cat.parent is not None else cat.name


class RulesView(QWidget):

    def __init__(self, user: User, session: Session,
                 theme: str = "light") -> None:
        super().__init__()
        self._user    = user
        self._session = session
        self._theme   = theme
        self._t       = THEMES[theme]
        self._rules:  List[AutoCategorizationRule] = []
        self._rows:   List[RuleRow] = []          # all visible rules (unfiltered)
        self._shown:  List[RuleRow] = []          # after search + filters
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
        self._filter_bar = self._make_filter_bar()
        root.addWidget(self._filter_bar)

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
            "Prioridad", "Patrón", "Tipo match", "Movimiento",
            "Categoría", "Ámbito", "Transacciones vinculadas",
            "Total (€)",
        ])
        # Numbers sort as numbers, text without case/accents (SORT_ROLE).
        self._model.setSortRole(SORT_ROLE)
        self._table.setModel(self._model)
        self._table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self._table.doubleClicked.connect(lambda _index: self._on_edit_rule())

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

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

    def _make_filter_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("filterBar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)

        self._inp_search = QLineEdit()
        self._inp_search.setPlaceholderText("Buscar reglas…")
        self._inp_search.setClearButtonEnabled(True)
        self._inp_search.setToolTip(
            "Busca cada palabra en el patrón, tipo de coincidencia, movimiento,\n"
            "categoría, tipo, ámbito, fecha de creación, prioridad y nº de movimientos.\n"
            "Sin distinguir mayúsculas ni acentos; todas las palabras deben aparecer."
        )
        self._inp_search.setFixedHeight(34)
        self._inp_search.setMinimumWidth(220)
        self._inp_search.setObjectName("filterInput")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._on_filter_changed)
        self._inp_search.textChanged.connect(self._search_timer.start)
        self._inp_search.returnPressed.connect(self._on_filter_changed)
        layout.addWidget(self._inp_search, 1)

        def combo(first: str, tooltip: str, min_width: int = 0) -> QComboBox:
            cmb = QComboBox()
            cmb.setFixedHeight(34)
            cmb.setObjectName("filterCombo")
            cmb.setToolTip(tooltip)
            if min_width:
                cmb.setMinimumWidth(min_width)
            cmb.addItem(first, None)
            cmb.currentIndexChanged.connect(self._on_filter_changed)
            layout.addWidget(cmb)
            return cmb

        self._cmb_cat = combo("Todas las categorías", "Categoría que asigna la regla", 170)
        self._cmb_type = combo("Todos los tipos", "Tipo de la categoría (Gasto, Ingreso…)")
        self._cmb_match = combo("Toda coincidencia", "Tipo de coincidencia")
        for mt, label in MATCH_TYPE_LABELS.items():
            self._cmb_match.addItem(label, mt)
        self._cmb_movement = combo("Todo movimiento", "Movimientos a los que se aplica la regla")
        for value, label in MOVEMENT_FILTERS.items():
            self._cmb_movement.addItem(label, value)
        self._cmb_scope = combo("Todo ámbito", "Global (toda la familia) o personal")
        self._cmb_scope.addItem("Global", SCOPE_GLOBAL)
        self._cmb_scope.addItem("Personal", SCOPE_PERSONAL)
        self._cmb_usage = combo("Cualquier uso", "Si la regla clasifica hoy algún movimiento")
        self._cmb_usage.addItem("Con movimientos", USAGE_WITH)
        self._cmb_usage.addItem("Sin movimientos", USAGE_WITHOUT)

        btn_clear = QPushButton("Limpiar filtros")
        btn_clear.setFixedHeight(34)
        btn_clear.setObjectName("btnSecondary")
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.clicked.connect(self._clear_filters)
        layout.addWidget(btn_clear)
        self._btn_clear = btn_clear
        return bar

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_categories(self) -> None:
        self._categories = get_all_categories(
            self._session, self._user.id, level=CategoryLevel.category
        )

    def refresh(self) -> None:
        """Reload rules and their usage stats (dry run of the engine), then
        re-apply the current search/filters. Only called after a change or
        an explicit action — filtering itself never re-runs the engine."""
        try:
            self._rules = get_all_rules(self._session, user_id=self._user.id)
            rule_transactions = self._get_rule_transactions()
            self._rows = build_rule_rows(
                self._session, self._user.id,
                matches={rid: len(txns) for rid, txns in rule_transactions.items()},
                totals={rid: sum(t.amount for t in txns)
                        for rid, txns in rule_transactions.items()},
                rules=self._rules,
            )
        except Exception:
            self._session.rollback()
            logger.exception("Error al cargar las reglas (user_id=%s)", self._user.id)
            self._rules, self._rows = [], []
        self._refresh_filter_options()
        self._populate()

    def _refresh_filter_options(self) -> None:
        """Fill the category/type combos from the rules, keeping the choice."""
        categories, types = filter_options(self._rows)
        for cmb, items in (
            (self._cmb_cat, [(f"{name} ({type_name})" if type_name else name, cid)
                             for cid, name, type_name in categories]),
            (self._cmb_type, [(name, name) for name in types]),
        ):
            keep = cmb.currentData()
            cmb.blockSignals(True)
            while cmb.count() > 1:
                cmb.removeItem(1)
            for label, value in items:
                cmb.addItem(label, value)
            index = cmb.findData(keep) if keep is not None else 0
            cmb.setCurrentIndex(max(0, index))
            cmb.blockSignals(False)

    def current_filters(self) -> RuleFilters:
        return RuleFilters(
            text=self._inp_search.text(),
            category_id=self._cmb_cat.currentData(),
            type_name=self._cmb_type.currentData(),
            match_type=self._cmb_match.currentData(),
            scope=self._cmb_scope.currentData(),
            usage=self._cmb_usage.currentData(),
            movement=self._cmb_movement.currentData(),
        )

    def _populate(self) -> None:
        """Show the rows passing the search/filters (no DB access)."""
        t = self._t
        filters = self.current_filters()
        self._shown = filter_rule_rows(self._rows, filters)

        match_colors = {
            MatchType.contains: t["accent"],
            MatchType.exact:    t["income"],
            MatchType.regex:    t["investment"],
        }
        center = Qt.AlignmentFlag.AlignCenter
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        def cell(text: str, sort_key, align=None) -> QStandardItem:
            item = QStandardItem(text)
            item.setData(sort_key, SORT_ROLE)
            item.setEditable(False)
            if align is not None:
                item.setTextAlignment(align)
            return item

        self._model.removeRows(0, self._model.rowCount())
        for row in self._shown:
            # Ties keep priority order: the key includes the rule id.
            item_pri = cell(str(row.priority), row.priority * 10_000_000 + row.id, center)
            item_pri.setData(row.id, RULE_ID_ROLE)

            item_pat = cell(row.pattern, fold_text(row.pattern))

            item_mt = cell(row.match_label, row.match_label, center)
            item_mt.setForeground(QColor(match_colors.get(row.match_type, t["text_primary"])))
            item_mt.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))

            item_mov = cell(row.movement_label, row.movement_label, center)
            if row.transaction_type == TransactionType.income:
                item_mov.setForeground(QColor(t["income"]))
            elif row.transaction_type == TransactionType.expense:
                item_mov.setForeground(QColor(t["expense"]))
            else:
                item_mov.setForeground(QColor(t["text_secondary"]))

            cat_name = row.category_name or "—"
            item_cat = cell(cat_name, fold_text(cat_name))
            if row.type_name:
                item_cat.setToolTip(f"{row.type_name} › {cat_name}")
            if row.category_id is None:
                item_cat.setForeground(QColor(t["warning"]))
                item_cat.setToolTip(
                    "Regla global sin categoría equivalente en tus categorías: "
                    "no se aplica a tus movimientos."
                )

            scope = "Global" if row.is_global else "Personal"
            item_scope = cell(scope, scope, center)
            item_scope.setForeground(
                QColor(t["investment"] if row.is_global else t["text_secondary"])
            )

            matches = row.matches or 0
            item_linked = cell(str(matches), matches, center)
            total = row.total or 0.0
            item_total = cell(_format_amount(total), total, right)

            self._model.appendRow([
                item_pri, item_pat, item_mt, item_mov, item_cat, item_scope,
                item_linked, item_total,
            ])

        # Keep the column the user sorted by (rebuilding the rows drops it).
        hdr = self._table.horizontalHeader()
        self._model.sort(hdr.sortIndicatorSection(), hdr.sortIndicatorOrder())

        self._lbl_count.setText(
            count_label(len(self._shown), len(self._rows), not filters.is_empty())
        )
        self._btn_clear.setEnabled(not filters.is_empty())

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

        # The model holds only the filtered, sorted rows: always resolve the
        # rule by the id stored in column 0 of that row, never by position.
        item = self._model.item(idx.row(), 0)
        if item is None:
            return None
        rule_id = item.data(RULE_ID_ROLE)
        return next((rule for rule in self._rules if rule.id == rule_id), None)

    # ------------------------------------------------------------------
    # Search / filters
    # ------------------------------------------------------------------

    def _on_filter_changed(self) -> None:
        self._search_timer.stop()
        self._populate()

    def _clear_filters(self) -> None:
        self._search_timer.stop()
        widgets = (self._inp_search, self._cmb_cat, self._cmb_type,
                   self._cmb_match, self._cmb_movement, self._cmb_scope, self._cmb_usage)
        for widget in widgets:
            widget.blockSignals(True)
        self._inp_search.clear()
        for cmb in widgets[1:]:
            cmb.setCurrentIndex(0)
        for widget in widgets:
            widget.blockSignals(False)
        self._populate()

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

        # Movement type
        cmb_movement = _movement_combo()
        form.addRow("Movimiento:", cmb_movement)

        # Category
        cmb_cat = QComboBox()
        cmb_cat.setFixedHeight(38)
        for cat in self._categories:
            cmb_cat.addItem(_category_label(cat), cat.id)
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
        if shares_global_rules(self._session, self._user.id):
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
        movement = cmb_movement.currentData()

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
                transaction_type = movement,
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

        # Movement type — pre-selected
        cmb_movement = _movement_combo(rule.transaction_type)
        form.addRow("Movimiento:", cmb_movement)

        # Category — pre-selected
        cmb_cat = QComboBox()
        cmb_cat.setFixedHeight(38)
        for cat in self._categories:
            cmb_cat.addItem(_category_label(cat), cat.id)
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
        movement = cmb_movement.currentData()

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
                transaction_type = movement,
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
            QFrame#filterBar {{
                background-color: {t["bg_primary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLineEdit#filterInput, QComboBox#filterCombo {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 6px;
                padding: 4px 10px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QLineEdit#filterInput:focus, QComboBox#filterCombo:focus {{
                border-color: {t["accent"]};
            }}
            QPushButton#btnSecondary:disabled {{
                color: {t["text_secondary"]};
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