"""
app/gui/investments_view.py
===========================
FAMILYFIN — Inversiones view (per user).

Layout
  top bar   title · price status ("Precios de las 18:52") · refresh · "+ Operación"
  tabs      Posiciones  (F4 — placeholder until then)
            Operaciones (ledger: filter bar + sortable table, edit/delete)

Prices are refreshed in the background by a PriceRefresher when the view
is shown and they are older than 15 minutes, or on demand. The GUI never
waits for the network: tables read the local cache only.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import qtawesome as qta
from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.config import THEMES
from app.db.models import InvestmentKind, InvestmentTransaction, User
from app.gui._format import fmt_date, fmt_eur, fmt_money, fmt_quantity
from app.gui._style_utils import apply_card_shadow
from app.gui.investment_dialogs import TransactionDialog
from app.investments.ledger import (
    KIND_LABELS,
    delete_transaction,
    known_brokers,
    list_transactions,
)
from app.investments.market_data import MarketDataProvider, YahooProvider
from app.investments.portfolio import LedgerError
from app.investments.prices import RefreshResult
from app.investments.refresh_worker import PriceRefresher

logger = logging.getLogger(__name__)

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2

# Cash direction seen from the user's pocket: buys take money out.
_CASH_SIGN = {
    InvestmentKind.buy: -1, InvestmentKind.grant: -1,
    InvestmentKind.sell: 1, InvestmentKind.dividend: 1, InvestmentKind.split: 0,
}

COLUMNS = ["Fecha", "Tipo", "Valor", "Bróker", "Acciones", "Precio",
           "Importe (EUR)", "Comisiones", "Notas"]


class InvestmentsView(QWidget):

    def __init__(
        self,
        user: User,
        session: Session,
        theme: str = "light",
        provider: Optional[MarketDataProvider] = None,
        refresher: Optional[PriceRefresher] = None,
    ) -> None:
        super().__init__()
        self._user = user
        self._session = session
        self._theme = theme
        self._t = THEMES[theme]
        self._provider = provider or YahooProvider()
        # The worker thread opens its own Session, bound to the same database
        # as the view's (not the module-level default, which a data-folder
        # switch or a test can leave pointing elsewhere).
        bind = session.get_bind()
        self._refresher = refresher or PriceRefresher(
            provider=self._provider, session_factory=lambda: Session(bind=bind), parent=self
        )
        self._refresher.started.connect(self._on_refresh_started)
        self._refresher.finished.connect(self._on_refresh_finished)

        self._build_ui()
        self._apply_theme()
        self.reload()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._make_topbar())

        self._tabs = QTabWidget()
        self._tabs.setObjectName("invTabs")
        self._tabs.setDocumentMode(True)
        self._tabs.addTab(self._make_positions_placeholder(), "Posiciones")
        self._tabs.addTab(self._make_operations_tab(), "Operaciones")
        self._tabs.setCurrentIndex(1)
        root.addWidget(self._tabs, 1)

    def _make_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("dashTopBar")
        bar.setFixedHeight(56)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(10)

        title = QLabel("Inversiones")
        title.setObjectName("dashTitle")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        layout.addWidget(title)
        layout.addStretch()

        self._lbl_status = QLabel("")
        self._lbl_status.setObjectName("invStatus")
        layout.addWidget(self._lbl_status)

        self._btn_refresh = QPushButton()
        self._btn_refresh.setFixedSize(32, 32)
        self._btn_refresh.setFlat(True)
        self._btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_refresh.setToolTip("Actualizar cotizaciones")
        self._btn_refresh.clicked.connect(self._refresher.refresh)
        layout.addWidget(self._btn_refresh)

        self._btn_add = QPushButton(" Operación")
        self._btn_add.setObjectName("btnPrimary")
        self._btn_add.setFixedHeight(34)
        self._btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add.setToolTip("Registrar una compra, venta, dividendo o split")
        self._btn_add.clicked.connect(lambda: self.open_transaction_dialog())
        layout.addWidget(self._btn_add)
        return bar

    def _make_positions_placeholder(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        self._lbl_positions_icon = QLabel()
        self._lbl_positions_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._lbl_positions_icon)
        text = QLabel("La vista de posiciones, con el valor actual de la cartera, "
                      "llegará en la siguiente fase.\nMientras tanto puedes ir "
                      "registrando tus operaciones.")
        text.setObjectName("emptyText")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(text)
        layout.addStretch()
        return page

    def _make_operations_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._filter_bar = QFrame()
        self._filter_bar.setObjectName("filterBar")
        self._filter_bar.setFixedHeight(52)
        bar = QHBoxLayout(self._filter_bar)
        bar.setContentsMargins(16, 8, 16, 8)
        bar.setSpacing(10)

        self._inp_search = QLineEdit()
        self._inp_search.setObjectName("filterInput")
        self._inp_search.setPlaceholderText("🔍  Buscar valor o nota…")
        self._inp_search.setFixedHeight(34)
        self._inp_search.setMinimumWidth(220)
        self._inp_search.textChanged.connect(self._load_operations)
        bar.addWidget(self._inp_search)

        self._cmb_kind = QComboBox()
        self._cmb_kind.setObjectName("filterCombo")
        self._cmb_kind.setFixedHeight(34)
        self._cmb_kind.addItem("Todos los tipos", None)
        for kind in (InvestmentKind.buy, InvestmentKind.sell, InvestmentKind.dividend,
                     InvestmentKind.split):
            self._cmb_kind.addItem(KIND_LABELS[kind], kind)
        self._cmb_kind.currentIndexChanged.connect(self._load_operations)
        bar.addWidget(self._cmb_kind)

        self._cmb_broker = QComboBox()
        self._cmb_broker.setObjectName("filterCombo")
        self._cmb_broker.setFixedHeight(34)
        self._cmb_broker.setMinimumWidth(150)
        self._cmb_broker.currentIndexChanged.connect(self._load_operations)
        bar.addWidget(self._cmb_broker)

        bar.addStretch()

        self._btn_edit = QPushButton("Editar")
        self._btn_edit.setObjectName("btnSecondary")
        self._btn_edit.setFixedHeight(34)
        self._btn_edit.clicked.connect(self._edit_selected)
        bar.addWidget(self._btn_edit)

        self._btn_delete = QPushButton("Eliminar")
        self._btn_delete.setObjectName("btnSecondary")
        self._btn_delete.setFixedHeight(34)
        self._btn_delete.clicked.connect(self._delete_selected)
        bar.addWidget(self._btn_delete)
        layout.addWidget(self._filter_bar)

        self._ops_stack = QStackedWidget()
        self._ops_stack.addWidget(self._make_empty_state())

        self._model = QStandardItemModel(0, len(COLUMNS))
        self._model.setHorizontalHeaderLabels(COLUMNS)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(SORT_ROLE)

        self._table = QTableView()
        self._table.setObjectName("invTable")
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.doubleClicked.connect(lambda _: self._edit_selected())
        header = self._table.horizontalHeader()
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.selectionModel().selectionChanged.connect(self._update_action_buttons)
        QShortcut(QKeySequence.StandardKey.Delete, self._table, activated=self._delete_selected)
        self._ops_stack.addWidget(self._table)
        layout.addWidget(self._ops_stack, 1)

        footer = QFrame()
        footer.setObjectName("paginationBar")
        footer.setFixedHeight(36)
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(16, 0, 16, 0)
        self._lbl_count = QLabel("")
        self._lbl_count.setObjectName("pageLabel")
        foot.addWidget(self._lbl_count)
        foot.addStretch()
        layout.addWidget(footer)
        return page

    def _make_empty_state(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        self._lbl_empty_icon = QLabel()
        self._lbl_empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._lbl_empty_icon)
        text = QLabel("Todavía no has registrado ninguna operación.\n"
                      "Empieza por las compras de tus acciones y fondos.")
        text.setObjectName("emptyText")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(text)
        row = QHBoxLayout()
        row.addStretch()
        button = QPushButton(" Añadir la primera operación")
        button.setObjectName("btnPrimary")
        button.setFixedHeight(36)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self.open_transaction_dialog())
        self._btn_empty_add = button
        row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return page

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def reload(self) -> None:
        self._load_brokers()
        self._load_operations()
        self._update_status()

    def _load_brokers(self) -> None:
        current = self._cmb_broker.currentData()
        used = {b for (b,) in self._session.query(InvestmentTransaction.broker)
                .filter(InvestmentTransaction.user_id == self._user.id).distinct()}
        self._cmb_broker.blockSignals(True)
        self._cmb_broker.clear()
        self._cmb_broker.addItem("Todos los brókers", None)
        for broker in known_brokers(self._session, self._user.id):
            if broker in used:
                self._cmb_broker.addItem(broker, broker)
        index = self._cmb_broker.findData(current) if current else 0
        self._cmb_broker.setCurrentIndex(max(index, 0))
        self._cmb_broker.blockSignals(False)

    def _load_operations(self, *_args) -> None:
        t = self._t
        rows = list_transactions(
            self._session, self._user.id,
            kind=self._cmb_kind.currentData(),
            broker=self._cmb_broker.currentData(),
            text=self._inp_search.text().strip() or None,
        )
        any_operation = (self._session.query(InvestmentTransaction.id)
                         .filter(InvestmentTransaction.user_id == self._user.id)
                         .first() is not None)
        self._ops_stack.setCurrentIndex(1 if any_operation else 0)
        self._filter_bar.setVisible(any_operation)

        sort_column = self._proxy.sortColumn()
        sort_order = self._proxy.sortOrder()
        self._table.setSortingEnabled(False)
        self._model.removeRows(0, self._model.rowCount())
        for txn in rows:
            self._model.appendRow(self._make_row(txn, t))
        self._table.setSortingEnabled(True)
        if sort_column < 0:
            self._table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        else:
            self._table.sortByColumn(sort_column, sort_order)

        total = len(rows)
        self._lbl_count.setText(f"{total} operación" if total == 1 else f"{total} operaciones")
        self._update_action_buttons()

    def _make_row(self, txn: InvestmentTransaction, t: dict) -> List[QStandardItem]:
        kind = InvestmentKind(txn.kind)
        security = txn.security

        def cell(text: str, sort=None, align_right=False, color=None, tooltip=None):
            item = QStandardItem(text)
            item.setData(sort if sort is not None else text.lower(), SORT_ROLE)
            item.setData(txn.id, ID_ROLE)
            if align_right:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if color:
                item.setForeground(QColor(color))
            if tooltip:
                item.setToolTip(tooltip)
            return item

        kind_colors = {
            InvestmentKind.buy: t["accent"], InvestmentKind.sell: t["warning"],
            InvestmentKind.dividend: t["income"], InvestmentKind.split: t["neutral"],
            InvestmentKind.grant: t["investment"],
        }
        cash = _CASH_SIGN[kind] * (txn.amount_eur or 0.0)
        if kind == InvestmentKind.split:
            quantity_text = f"× {fmt_quantity(txn.split_ratio)}"
            amount_text = "—"
        else:
            quantity_text = fmt_quantity(txn.quantity)
            amount_text = fmt_eur(cash, signed=True)
        amount_tip = None
        if kind == InvestmentKind.dividend and txn.tax_withheld_eur:
            amount_tip = f"Retención en origen: {fmt_eur(txn.tax_withheld_eur)}"
        if kind == InvestmentKind.grant and txn.cash_paid_eur is not None:
            amount_tip = f"Pagado de tu bolsillo: {fmt_eur(txn.cash_paid_eur)}"

        bold = QFont()
        bold.setBold(True)
        kind_item = cell(KIND_LABELS[kind], color=kind_colors.get(kind))
        kind_item.setFont(bold)
        return [
            cell(fmt_date(txn.trade_date), sort=txn.trade_date.toordinal() * 1_000_000 + txn.id),
            kind_item,
            cell(f"{security.name}  ·  {security.symbol}", tooltip=security.exchange),
            cell(txn.broker),
            cell(quantity_text, sort=txn.quantity or 0.0, align_right=True),
            cell(fmt_money(txn.price, txn.currency, 4 if (txn.price or 0) < 10 else 2)
                 if txn.price else "—", sort=txn.price or 0.0, align_right=True),
            cell(amount_text, sort=cash, align_right=True,
                 color=(t["income"] if cash > 0 else t["expense"] if cash < 0 else None),
                 tooltip=amount_tip),
            cell(fmt_eur(txn.fees_eur) if txn.fees_eur else "—", sort=txn.fees_eur or 0.0,
                 align_right=True),
            cell(txn.notes or "", tooltip=txn.notes),
        ]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _selected_id(self) -> Optional[int]:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._proxy.data(indexes[0], ID_ROLE)

    def _update_action_buttons(self, *_args) -> None:
        selected = self._selected_id() is not None
        self._btn_edit.setEnabled(selected)
        self._btn_delete.setEnabled(selected)

    def open_transaction_dialog(self, transaction: Optional[InvestmentTransaction] = None) -> bool:
        dialog = TransactionDialog(self._session, self._user.id, self._provider, self._t,
                                   transaction=transaction, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            if dialog.created_security:
                self.reload()
            return False
        self.reload()
        # New security or a date before the cached history → fetch prices now.
        self._refresher.refresh()
        return True

    def _edit_selected(self) -> None:
        txn_id = self._selected_id()
        if txn_id is None:
            return
        txn = self._session.get(InvestmentTransaction, txn_id)
        if txn is not None and txn.user_id == self._user.id:
            self.open_transaction_dialog(txn)

    def _delete_selected(self) -> None:
        txn_id = self._selected_id()
        if txn_id is None:
            return
        txn = self._session.get(InvestmentTransaction, txn_id)
        if txn is None:
            return
        summary = (f"{KIND_LABELS[InvestmentKind(txn.kind)]} de {txn.security.name} "
                   f"del {fmt_date(txn.trade_date)}")
        answer = QMessageBox.question(
            self, "Eliminar operación",
            f"¿Eliminar la operación «{summary}»?\nEsta acción no se puede deshacer.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_transaction(self._session, self._user.id, txn_id)
        except LedgerError as exc:
            QMessageBox.warning(self, "No se puede eliminar", str(exc))
            return
        self.reload()

    def _show_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        self._table.selectRow(index.row())
        menu = QMenu(self)
        menu.addAction(qta.icon("mdi.pencil-outline", color=self._t["text_secondary"]),
                       "Editar…", self._edit_selected)
        menu.addAction(qta.icon("mdi.delete-outline", color=self._t["expense"]),
                       "Eliminar…", self._delete_selected)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Price refresh
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.reload()
        self._refresher.refresh_if_stale()

    def _on_refresh_started(self) -> None:
        self._btn_refresh.setEnabled(False)
        self._update_status()

    def _on_refresh_finished(self, result: RefreshResult) -> None:
        self._btn_refresh.setEnabled(True)
        if result.missing:
            logger.info("Sin cotización para: %s", ", ".join(result.missing))
        self._update_status()

    def _update_status(self) -> None:
        t = self._t
        refresher = self._refresher
        if refresher.running:
            text, color = "Actualizando cotizaciones…", t["text_secondary"]
        elif refresher.last_result is not None and not refresher.last_result.ok:
            last = refresher.last_success
            text = "Sin conexión con Yahoo Finance"
            if last:
                text += f" · precios de las {last:%H:%M}"
            else:
                text += " · se muestran los últimos precios guardados"
            color = t["warning"]
        elif refresher.last_success is not None:
            text, color = f"Cotizaciones actualizadas a las {refresher.last_success:%H:%M}", \
                t["text_secondary"]
        else:
            text, color = "", t["text_secondary"]
        self._lbl_status.setText(text)
        self._lbl_status.setToolTip(refresher.last_result.error
                                    if refresher.last_result and refresher.last_result.error
                                    else "")
        self._lbl_status.setStyleSheet(f"color: {color}; font-size: 12px;")

    def shutdown(self) -> None:
        """Called by the main window on close: let a running refresh finish."""
        self._refresher.shutdown()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------
    def apply_theme(self, theme_key: str) -> None:
        self._theme = theme_key
        self._t = THEMES[theme_key]
        self._apply_theme()
        self._load_operations()
        self._update_status()

    def _apply_theme(self) -> None:
        t = self._t
        is_dark = self._theme == "dark"
        selection_bg = (
            f"background-color: {t['accent']}; color: #FFFFFF;" if is_dark else
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 {t['selection_grad_start']}, stop:1 {t['selection_grad_end']}); "
            f"color: {t['selection_text']};"
        )
        self._btn_refresh.setIcon(qta.icon("mdi.refresh", color=t["text_secondary"]))
        self._btn_add.setIcon(qta.icon("mdi.plus", color="#FFFFFF"))
        self._btn_empty_add.setIcon(qta.icon("mdi.plus", color="#FFFFFF"))
        self._btn_edit.setIcon(qta.icon("mdi.pencil-outline", color=t["text_secondary"]))
        self._btn_delete.setIcon(qta.icon("mdi.delete-outline", color=t["text_secondary"]))
        self._lbl_empty_icon.setPixmap(
            qta.icon("mdi.chart-line", color=t["investment"]).pixmap(56, 56))
        self._lbl_positions_icon.setPixmap(
            qta.icon("mdi.briefcase-outline", color=t["text_secondary"]).pixmap(48, 48))
        self.setStyleSheet(f"""
            QWidget {{ background-color: {t["bg_primary"]}; }}
            QLabel {{ background: transparent; }}
            QFrame#dashTopBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLabel#dashTitle {{
                color: {t["text_primary"]}; font-size: 16px; font-weight: 700;
                background: transparent;
            }}
            QLabel#emptyText {{ color: {t["text_secondary"]}; font-size: 13px; }}
            QTabWidget::pane {{ border: none; }}
            QTabBar::tab {{
                background: transparent; color: {t["text_secondary"]};
                padding: 10px 18px; font-size: 13px; font-weight: 600;
                border: none; border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{
                color: {t["accent"]}; border-bottom: 2px solid {t["accent"]};
            }}
            QTabBar::tab:hover {{ color: {t["text_primary"]}; }}
            QFrame#filterBar {{
                background-color: {t["bg_primary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLineEdit#filterInput, QComboBox#filterCombo {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]}; border-radius: 6px;
                padding: 4px 10px; color: {t["text_primary"]}; font-size: 13px;
            }}
            QLineEdit#filterInput:focus {{ border-color: {t["accent"]}; }}
            QPushButton#btnPrimary {{
                background-color: {t["accent"]}; color: #FFFFFF;
                border: none; border-radius: 6px;
                font-size: 13px; font-weight: 600; padding: 0 16px;
            }}
            QPushButton#btnPrimary:hover {{ background-color: {t["accent_hover"]}; }}
            QPushButton#btnSecondary {{
                background-color: transparent; color: {t["text_secondary"]};
                border: 1px solid {t["border"]}; border-radius: 6px;
                font-size: 12px; padding: 0 12px;
            }}
            QPushButton#btnSecondary:hover {{ background-color: {t["bg_hover"]}; }}
            QPushButton#btnSecondary:disabled {{ color: {t["border_strong"]}; }}
            QTableView#invTable {{
                background-color: {t["bg_secondary"]}; border: none;
                color: {t["text_primary"]}; font-size: 12px;
                alternate-background-color: {t["bg_primary"]};
            }}
            QTableView#invTable::item {{
                padding: 6px 8px; border-bottom: 1px solid {t["border"]};
            }}
            QTableView#invTable::item:selected {{ {selection_bg} }}
            QHeaderView::section {{
                background-color: {t["bg_primary"]}; color: {t["text_secondary"]};
                font-size: 11px; font-weight: 700; padding: 6px 8px; border: none;
                border-bottom: 2px solid {t["border"]};
            }}
            QFrame#paginationBar {{
                background-color: {t["bg_secondary"]};
                border-top: 1px solid {t["border"]};
            }}
            QLabel#pageLabel {{ color: {t["text_secondary"]}; font-size: 12px; }}
        """)
        apply_card_shadow(self._filter_bar, t, blur_radius=12, y_offset=2)
