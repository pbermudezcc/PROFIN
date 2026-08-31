"""
app/gui/transactions_view.py
=============================
PROFIN — Transactions View (Fase 4b)

Features:
  - Filterable/sortable table: Fecha | Emisor | Concepto | Importe
                               | Tipo | Categoría | Saldo
  - Inline category assignment via dropdown in Categoría column
  - Filter bar: search text + type filter + date range
  - Pagination for large datasets (100 rows per page)
"""
from __future__ import annotations

from datetime import date
from typing   import List, Optional

from PySide6.QtCore    import Qt, QSortFilterProxyModel, QDate
from PySide6.QtGui     import QFont, QColor, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QTableView, QHeaderView,
    QLineEdit, QComboBox, QSizePolicy, QAbstractItemView,
    QDateEdit, QApplication,
)
import qtawesome as qta

from sqlalchemy        import or_
from sqlalchemy.orm    import Session

from app.config      import THEMES
from app.db.models   import (
    Transaction, TransactionType, Category, CategoryLevel, User
)
from app.categorization.category_manager import (
    assign_transaction_category, get_all_categories
)


PAGE_SIZE = 100


class TransactionsView(QWidget):

    def __init__(self, user: User, session: Session,
                 theme: str = "light") -> None:
        super().__init__()
        self._user    = user
        self._session = session
        self._theme   = theme
        self._t       = THEMES[theme]
        self._page    = 0
        self._total   = 0
        self._categories: List[Category] = []

        self._build_ui()
        self._apply_theme()
        self._load_categories()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Top bar
        root.addWidget(self._make_topbar())

        # Filter bar
        root.addWidget(self._make_filter_bar())

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
        self._table.verticalHeader().setVisible(False)
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setStretchLastSection(False)

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels([
            "Fecha", "Emisor", "Concepto",
            "Importe", "Tipo", "Categoría", "Saldo"
        ])
        self._table.setModel(self._model)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)

        self._table.doubleClicked.connect(self._on_row_double_clicked)
        root.addWidget(self._table)

        # Pagination bar
        root.addWidget(self._make_pagination_bar())

    def _make_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("dashTopBar")
        bar.setFixedHeight(56)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)

        lbl = QLabel("Transacciones")
        lbl.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        lbl.setObjectName("dashTitle")
        layout.addWidget(lbl)

        layout.addStretch()

        self._lbl_count = QLabel("")
        self._lbl_count.setObjectName("txnCount")
        layout.addWidget(self._lbl_count)

        layout.addSpacing(12)

        btn_refresh = QPushButton()
        btn_refresh.setFixedSize(32, 32)
        btn_refresh.setFlat(True)
        btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refresh.setToolTip("Actualizar — re-aplica reglas y recarga tabla")
        btn_refresh.setIcon(
            qta.icon("mdi.refresh", color=self._t["text_secondary"])
        )
        btn_refresh.clicked.connect(self._refresh_with_rules)
        layout.addWidget(btn_refresh)
        self._btn_refresh_top = btn_refresh

        return bar

    def _make_filter_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("filterBar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)

        # Search
        self._inp_search = QLineEdit()
        self._inp_search.setPlaceholderText("🔍  Buscar por emisor o concepto...")
        self._inp_search.setFixedHeight(34)
        self._inp_search.setObjectName("filterInput")
        self._inp_search.setMinimumWidth(240)
        self._inp_search.textChanged.connect(self._on_filter_changed)
        layout.addWidget(self._inp_search)

        # Type filter
        self._cmb_type = QComboBox()
        self._cmb_type.setFixedHeight(34)
        self._cmb_type.setObjectName("filterCombo")
        self._cmb_type.addItem("Todos los tipos", None)
        self._cmb_type.addItem("Ingresos",   TransactionType.income)
        self._cmb_type.addItem("Gastos",     TransactionType.expense)
        self._cmb_type.addItem("Inversiones",TransactionType.investment)
        self._cmb_type.currentIndexChanged.connect(self._on_filter_changed)
        layout.addWidget(self._cmb_type)

        # Category filter
        self._cmb_cat = QComboBox()
        self._cmb_cat.setFixedHeight(34)
        self._cmb_cat.setObjectName("filterCombo")
        self._cmb_cat.setMinimumWidth(160)
        self._cmb_cat.addItem("Todas las categorías", None)
        self._cmb_cat.addItem("Sin categorizar", -1)
        self._cmb_cat.currentIndexChanged.connect(self._on_filter_changed)
        layout.addWidget(self._cmb_cat)

        layout.addStretch()

        # Clear filters
        btn_clear = QPushButton("Limpiar filtros")
        btn_clear.setFixedHeight(34)
        btn_clear.setObjectName("btnSecondary")
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.clicked.connect(self._clear_filters)
        layout.addWidget(btn_clear)

        return bar

    def _make_pagination_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("paginationBar")
        bar.setFixedHeight(44)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(8)

        self._btn_prev = QPushButton()
        self._btn_prev.setFixedSize(32, 32)
        self._btn_prev.setFlat(True)
        self._btn_prev.setIcon(
            qta.icon("mdi.chevron-left", color=self._t["text_secondary"])
        )
        self._btn_prev.clicked.connect(self._prev_page)
        layout.addWidget(self._btn_prev)

        self._lbl_page = QLabel("Página 1")
        self._lbl_page.setObjectName("pageLabel")
        layout.addWidget(self._lbl_page)

        self._btn_next = QPushButton()
        self._btn_next.setFixedSize(32, 32)
        self._btn_next.setFlat(True)
        self._btn_next.setIcon(
            qta.icon("mdi.chevron-right", color=self._t["text_secondary"])
        )
        self._btn_next.clicked.connect(self._next_page)
        layout.addWidget(self._btn_next)

        layout.addStretch()
        return bar

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_categories(self) -> None:
        """Load categories for the inline dropdown."""
        self._categories = get_all_categories(
            self._session, self._user.id, level=CategoryLevel.category
        )
        # Refresh category filter combo
        self._cmb_cat.clear()
        self._cmb_cat.addItem("Todas las categorías", None)
        self._cmb_cat.addItem("Sin categorizar", -1)
        for cat in self._categories:
            self._cmb_cat.addItem(cat.name, cat.id)

    def _refresh_with_rules(self) -> None:
        """
        Re-apply all active categorization rules to uncategorized
        transactions, then reload the table.
        Called by the top-bar refresh button.
        """
        import logging
        _log = logging.getLogger(__name__)

        try:
            from app.db.models import Transaction as TxnModel
            from app.categorization.rules_engine import apply_rules_and_commit

            all_ids = [
                row.id for row in (
                    self._session.query(TxnModel.id)
                    .filter(TxnModel.user_id == self._user.id)
                    .all()
                )
            ]

            if all_ids:
                result = apply_rules_and_commit(
                    self._session,
                    all_ids,
                    self._user.id,
                    overwrite=True,
                )
                n = len(result.assigned)
                if n:
                    _log.info(
                        "Refresh: %d transacciones auto-categorizadas", n
                    )

        except Exception as exc:
            _log.warning("Error al re-aplicar reglas en refresh: %s", exc)

        # Always reload the table regardless of rule results
        self.refresh()

    def showEvent(self, event) -> None:
        """
        Called automatically by Qt every time this widget becomes visible
        — including when the user clicks 'Transacciones' in the sidebar.
        Re-applies categorization rules silently and reloads the table.
        """
        super().showEvent(event)
        self._refresh_with_rules()

    def refresh(self) -> None:
        self._page = 0
        self._load_categories()
        self._load_data()

    def _load_data(self) -> None:
        t = self._t
        search_text = self._inp_search.text().strip().lower()
        type_filter = self._cmb_type.currentData()
        cat_filter  = self._cmb_cat.currentData()

        query = (
            self._session.query(Transaction)
            .filter(Transaction.user_id == self._user.id)
        )

        if type_filter is not None:
            query = query.filter(Transaction.type == type_filter)

        if cat_filter == -1:
            query = query.filter(
                Transaction.category_id == None  # noqa: E711
            )
        elif cat_filter is not None:
            query = query.filter(Transaction.category_id == cat_filter)

        if search_text:
            query = query.filter(
                or_(
                    Transaction.issuer_name.ilike(f"%{search_text}%"),
                    Transaction.concept.ilike(f"%{search_text}%"),
                )
            )

        query = query.order_by(
            Transaction.transaction_date.desc(),
            Transaction.id.desc()
        )

        self._total = query.count()
        offset      = self._page * PAGE_SIZE
        transactions = query.offset(offset).limit(PAGE_SIZE).all()

        self._lbl_count.setText(
            f"{self._total} transacciones"
        )
        total_pages = max(1, (self._total + PAGE_SIZE - 1) // PAGE_SIZE)
        self._lbl_page.setText(
            f"Página {self._page + 1} de {total_pages}"
        )
        self._btn_prev.setEnabled(self._page > 0)
        self._btn_next.setEnabled(
            (self._page + 1) * PAGE_SIZE < self._total
        )

        # Build category lookup
        cat_map = {c.id: c.name for c in self._categories}
        type_labels = {
            TransactionType.income:     "Ingreso",
            TransactionType.expense:    "Gasto",
            TransactionType.investment: "Inversión",
            TransactionType.unknown:    "—",
        }

        self._model.removeRows(0, self._model.rowCount())
        self._txn_ids: List[int] = []

        for txn in transactions:
            self._txn_ids.append(txn.id)

            amount  = txn.amount or 0.0
            balance = txn.running_balance or 0.0

            # Fecha
            item_date = QStandardItem(
                str(txn.transaction_date) if txn.transaction_date else ""
            )

            # Emisor
            item_issuer = QStandardItem(
                (txn.issuer_name or "")[:40]
            )

            # Concepto
            item_concept = QStandardItem(
                (txn.concept or "")[:80]
            )

            # Importe
            amt_str = self._fmt_amount(amount)
            item_amount = QStandardItem(amt_str)
            item_amount.setTextAlignment(
                Qt.AlignmentFlag.AlignRight |
                Qt.AlignmentFlag.AlignVCenter
            )
            clr = t["income"] if amount >= 0 else t["expense"]
            item_amount.setForeground(QColor(clr))

            # Tipo
            item_type = QStandardItem(
                type_labels.get(txn.type, "—")
            )

            # Categoría
            cat_name = cat_map.get(txn.category_id, "Sin categorizar")
            item_cat = QStandardItem(cat_name)
            if txn.category_id is None:
                item_cat.setForeground(QColor(t["warning"]))

            # Saldo
            item_balance = QStandardItem(
                self._fmt_amount(balance)
            )
            item_balance.setTextAlignment(
                Qt.AlignmentFlag.AlignRight |
                Qt.AlignmentFlag.AlignVCenter
            )

            row = [item_date, item_issuer, item_concept,
                   item_amount, item_type, item_cat, item_balance]
            for item in row:
                item.setEditable(False)
            self._model.appendRow(row)

    # ------------------------------------------------------------------
    # Category assignment dialog (double-click on row)
    # ------------------------------------------------------------------

    def _on_row_double_clicked(self, index) -> None:
        row = index.row()
        if row >= len(self._txn_ids):
            return
        txn_id = self._txn_ids[row]
        self._open_category_dialog(txn_id, row)

    def _open_category_dialog(self, txn_id: int, row: int) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        t   = self._t
        txn = self._session.get(Transaction, txn_id)
        if txn is None:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Asignar categoría")
        dlg.setFixedWidth(400)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        # Transaction info
        lbl_info = QLabel(
            f"<b>{txn.issuer_name or txn.concept or 'Transacción'}</b><br>"
            f"<small>{txn.transaction_date} · "
            f"{self._fmt_amount(txn.amount or 0)}</small>"
        )
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet(
            f"color: {t['text_primary']}; font-size: 13px;"
        )
        layout.addWidget(lbl_info)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {t['border']};")
        layout.addWidget(sep)

        lbl_cat = QLabel("Categoría:")
        lbl_cat.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {t['text_secondary']};"
        )
        layout.addWidget(lbl_cat)

        cmb = QComboBox()
        cmb.setFixedHeight(38)
        cmb.addItem("Sin categorizar", None)
        current_idx = 0
        for i, cat in enumerate(self._categories, start=1):
            cmb.addItem(cat.name, cat.id)
            if cat.id == txn.category_id:
                current_idx = i
        cmb.setCurrentIndex(current_idx)
        cmb.setStyleSheet(f"""
            QComboBox {{
                background-color: {t["bg_primary"]};
                border: 1.5px solid {t["border"]};
                border-radius: 8px;
                padding: 6px 12px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QComboBox:focus {{ border-color: {t["accent"]}; }}
        """)
        layout.addWidget(cmb)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Guardar"
        )
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setObjectName(
            "btnPrimary"
        )
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addWidget(btn_box)

        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {t["bg_secondary"]}; }}
            QPushButton#btnPrimary {{
                background-color: {t["accent"]}; color: #FFFFFF;
                border: none; border-radius: 6px;
                font-size: 13px; font-weight: 600;
                padding: 6px 16px;
            }}
        """)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cat_id = cmb.currentData()
            assign_transaction_category(
                self._session, txn_id, new_cat_id
            )
            # Update model cell directly (no full reload)
            cat_name = next(
                (c.name for c in self._categories
                 if c.id == new_cat_id),
                "Sin categorizar"
            )
            self._model.item(row, 5).setText(cat_name)
            if new_cat_id is None:
                self._model.item(row, 5).setForeground(
                    QColor(t["warning"])
                )
            else:
                self._model.item(row, 5).setForeground(
                    QColor(t["text_primary"])
                )

    # ------------------------------------------------------------------
    # Filter / pagination slots
    # ------------------------------------------------------------------

    def _on_filter_changed(self) -> None:
        self._page = 0
        self._load_data()

    def _clear_filters(self) -> None:
        self._inp_search.clear()
        self._cmb_type.setCurrentIndex(0)
        self._cmb_cat.setCurrentIndex(0)
        self._page = 0
        self._load_data()

    def _prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._load_data()

    def _next_page(self) -> None:
        if (self._page + 1) * PAGE_SIZE < self._total:
            self._page += 1
            self._load_data()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_amount(value: float) -> str:
        sign = "+" if value >= 0 else ""
        return (
            f"{sign}{abs(value):,.2f} €"
            .replace(",", "X").replace(".", ",").replace("X", ".")
        )

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
            QLabel#txnCount {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QFrame#filterBar {{
                background-color: {t["bg_primary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLineEdit#filterInput {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 6px;
                padding: 4px 10px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QLineEdit#filterInput:focus {{
                border-color: {t["accent"]};
            }}
            QComboBox#filterCombo {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 6px;
                padding: 4px 10px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QPushButton#btnSecondary {{
                background-color: transparent;
                color: {t["text_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 6px;
                font-size: 12px;
                padding: 0 12px;
            }}
            QPushButton#btnSecondary:hover {{
                background-color: {t["bg_hover"]};
            }}
            QTableView {{
                background-color: {t["bg_secondary"]};
                border: none;
                color: {t["text_primary"]};
                font-size: 12px;
                alternate-background-color: {t["bg_primary"]};
            }}
            QTableView::item {{
                padding: 6px 8px;
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
            QFrame#paginationBar {{
                background-color: {t["bg_secondary"]};
                border-top: 1px solid {t["border"]};
            }}
            QLabel#pageLabel {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
        """)