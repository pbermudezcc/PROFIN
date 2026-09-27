"""
app/gui/investment_dialogs.py
=============================
FAMILYFIN — Dialogs of the Inversiones view.

SecuritySearchDialog
    Searches the market-data provider and adds the chosen security to the
    shared catalogue. Search and the one-off "security info" call run in a
    background thread; the database is touched only on the GUI thread.

TransactionDialog
    Create or edit one operation (Compra / Venta / Dividendo / Split).
    * The EUR total is pre-filled with an estimate (shares × price ÷ FX of
      that day ± fees) until the user types their own figure — the exact
      amount on the broker statement is what the tax return uses.
    * The close of the trade date is offered as the price ("Usar").
    * For a sale, the shares held on that date are shown ("Vender todas").
    * Saving goes through ledger.save_transaction, so an operation that
      would make the ledger impossible is rejected in the dialog itself.
    Market data comes from the local cache first; when the cache has no
    close/FX for the date, one small provider call fills the gap.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional, Tuple

import qtawesome as qta
from PySide6.QtCore import QDate, QLocale, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.db.models import AssetType, InvestmentKind, InvestmentTransaction, Security
from app.gui._background import BackgroundRunner
from app.gui._format import fmt_date, fmt_money, fmt_number, fmt_quantity
from app.investments.ledger import (
    KIND_LABELS,
    TransactionDraft,
    estimate_amount_eur,
    known_brokers,
    picker_securities,
    save_transaction,
)
from app.investments.market_data import (
    MarketDataError,
    MarketDataProvider,
    fx_pair,
    fx_symbol,
)
from app.investments.portfolio import LedgerError, build_positions
from app.investments.prices import close_on, fx_rate, security_from_info

ASSET_LABELS = {AssetType.stock: "Acción", AssetType.etf: "ETF", AssetType.fund: "Fondo"}

# A cached close/FX older than this before the trade date is not trusted
# for the estimate; the provider is asked instead.
_MAX_CACHE_GAP = timedelta(days=5)


def dialog_stylesheet(t: dict) -> str:
    return f"""
        QDialog {{ background-color: {t["bg_secondary"]}; }}
        QLabel {{ color: {t["text_primary"]}; font-size: 13px; background: transparent; }}
        QLabel#dlgTitle {{ font-size: 16px; font-weight: 700; }}
        QLabel#dlgHint {{ color: {t["text_secondary"]}; font-size: 12px; }}
        QLabel#dlgError {{ color: {t["expense"]}; font-size: 12px; font-weight: 600; }}
        QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox {{
            background-color: {t["bg_primary"]};
            border: 1px solid {t["border"]};
            border-radius: 6px;
            padding: 5px 8px;
            color: {t["text_primary"]};
            font-size: 13px;
            min-height: 22px;
        }}
        QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QDoubleSpinBox:focus {{
            border-color: {t["accent"]};
        }}
        QPushButton#btnPrimary {{
            background-color: {t["accent"]}; color: #FFFFFF;
            border: none; border-radius: 6px;
            font-size: 13px; font-weight: 600; padding: 7px 18px;
        }}
        QPushButton#btnPrimary:hover {{ background-color: {t["accent_hover"]}; }}
        QPushButton#btnPrimary:disabled {{ background-color: {t["border_strong"]}; }}
        QPushButton#btnSecondary, QPushButton#btnLink {{
            background-color: transparent; color: {t["text_secondary"]};
            border: 1px solid {t["border"]}; border-radius: 6px;
            font-size: 12px; padding: 5px 12px;
        }}
        QPushButton#btnLink {{ color: {t["accent"]}; border: none; padding: 2px 6px; }}
        QPushButton#btnSecondary:hover, QPushButton#btnLink:hover {{
            background-color: {t["bg_hover"]};
        }}
        QTableWidget {{
            background-color: {t["bg_secondary"]};
            border: 1px solid {t["border"]}; border-radius: 6px;
            color: {t["text_primary"]}; font-size: 12px;
            alternate-background-color: {t["bg_primary"]};
        }}
        QTableWidget::item:selected {{ background-color: {t["accent"]}; color: #FFFFFF; }}
        QHeaderView::section {{
            background-color: {t["bg_primary"]}; color: {t["text_secondary"]};
            font-size: 11px; font-weight: 700; padding: 5px 8px; border: none;
            border-bottom: 2px solid {t["border"]};
        }}
    """


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _pydate(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


_ES = QLocale(QLocale.Language.Spanish, QLocale.Country.Spain)


class _NumberBox(QDoubleSpinBox):
    """Spanish decimal comma; shares/prices drop trailing zeros (2 → "2")."""

    def __init__(self, decimals: int, trim: bool) -> None:
        super().__init__()
        self._trim = trim
        self.setLocale(_ES)
        self.setDecimals(decimals)

    def textFromValue(self, value: float) -> str:
        text = _ES.toString(value, "f", self.decimals())
        if self._trim and "," in text:
            text = text.rstrip("0").rstrip(",")
        return text


def _spin(decimals: int, suffix: str = "", maximum: float = 1e12,
          trim: bool = False) -> QDoubleSpinBox:
    spin = _NumberBox(decimals, trim)
    spin.setRange(0.0, maximum)
    spin.setSuffix(suffix)
    spin.setGroupSeparatorShown(True)
    spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight)
    spin.setFixedWidth(190)
    return spin


# ---------------------------------------------------------------------------
# Security search
# ---------------------------------------------------------------------------
class SecuritySearchDialog(QDialog):
    """Find a security with the provider and add it to the catalogue."""

    def __init__(self, session: Session, provider: MarketDataProvider,
                 theme: dict, initial_text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session = session
        self._provider = provider
        self._t = theme
        self._results = []
        self.security: Optional[Security] = None
        self._runner = BackgroundRunner(self)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self._search)

        self.setWindowTitle("Buscar valor")
        self.setModal(True)
        self.resize(620, 460)
        self._build()
        self.setStyleSheet(dialog_stylesheet(theme))
        if initial_text:
            self._inp.setText(initial_text)
            self._search()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title = QLabel("Buscar acción, ETF o fondo")
        title.setObjectName("dlgTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Escribe el ticker, el nombre o el ISIN. Fíjate en la bolsa: la misma "
            "empresa cotiza en varias (p. ej. AMZN en NASDAQ en USD, AMZ.DE en Xetra en EUR); "
            "elige la bolsa en la que compraste."
        )
        hint.setObjectName("dlgHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self._inp = QLineEdit()
        self._inp.setPlaceholderText("p. ej. Siemens Energy, GEV, Boreas…")
        self._inp.textChanged.connect(self._on_text_changed)
        self._inp.returnPressed.connect(self._search)
        row.addWidget(self._inp, 1)
        btn = QPushButton(" Buscar")
        btn.setObjectName("btnSecondary")
        btn.setIcon(qta.icon("mdi.magnify", color=self._t["text_secondary"]))
        btn.clicked.connect(self._search)
        row.addWidget(btn)
        layout.addLayout(row)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Símbolo", "Nombre", "Bolsa", "Tipo"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._update_buttons)
        self._table.doubleClicked.connect(lambda _: self._add())
        layout.addWidget(self._table, 1)

        form = QFormLayout()
        self._inp_isin = QLineEdit()
        self._inp_isin.setPlaceholderText("Opcional (12 caracteres)")
        self._inp_isin.setMaxLength(12)
        form.addRow("ISIN:", self._inp_isin)
        layout.addLayout(form)

        self._lbl_status = QLabel("")
        self._lbl_status.setObjectName("dlgHint")
        self._lbl_status.setWordWrap(True)
        layout.addWidget(self._lbl_status)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.setObjectName("btnSecondary")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._btn_add = QPushButton("Añadir valor")
        self._btn_add.setObjectName("btnPrimary")
        self._btn_add.clicked.connect(self._add)
        buttons.addWidget(self._btn_add)
        layout.addLayout(buttons)
        self._update_buttons()

    def _set_status(self, text: str, error: bool = False) -> None:
        self._lbl_status.setObjectName("dlgError" if error else "dlgHint")
        self._lbl_status.setStyleSheet("")      # re-evaluate the object-name rule
        self._lbl_status.setText(text)

    def _on_text_changed(self, text: str) -> None:
        if len(text.strip()) >= 2:
            self._debounce.start()

    def _search(self) -> None:
        self._debounce.stop()
        text = self._inp.text().strip()
        if not text:
            return
        self._set_status("Buscando…")
        provider = self._provider
        self._runner.run(lambda: provider.search(text), self._on_results, latest_only=True)

    def _on_results(self, results, error) -> None:
        if error is not None:
            self._set_status(str(error) if isinstance(error, MarketDataError)
                             else f"Error en la búsqueda: {error}", error=True)
            return
        self._results = list(results or [])
        self._table.setRowCount(len(self._results))
        for row, result in enumerate(self._results):
            values = [result.symbol, result.name, result.exchange,
                      ASSET_LABELS.get(result.asset_type, "")]
            for col, value in enumerate(values):
                self._table.setItem(row, col, QTableWidgetItem(value))
        if self._results:
            self._table.selectRow(0)
            self._set_status(f"{len(self._results)} resultados.")
        else:
            self._set_status("Sin resultados. Prueba con otro nombre o con el ticker.")
        self._update_buttons()

    def _update_buttons(self) -> None:
        self._btn_add.setEnabled(bool(self._table.selectedItems()) and not self._runner.busy)

    def _add(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        result = self._results[rows[0].row()]
        existing = (self._session.query(Security)
                    .filter(Security.symbol == result.symbol.upper()).one_or_none())
        if existing is not None:
            self._finish(existing)
            return
        self._set_status(f"Obteniendo los datos de {result.symbol}…")
        self._btn_add.setEnabled(False)
        provider = self._provider
        self._runner.run(lambda: provider.security_info(result.symbol), self._on_info)

    def _on_info(self, info, error) -> None:
        self._update_buttons()
        if error is not None:
            self._set_status(str(error), error=True)
            return
        try:
            security = security_from_info(self._session, info, self._inp_isin.text().strip() or None)
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            self._set_status(f"No se pudo guardar el valor: {exc}", error=True)
            return
        self._finish(security)

    def _finish(self, security: Security) -> None:
        self.security = security
        self.accept()

    def done(self, result: int) -> None:
        self._runner.shutdown()
        super().done(result)


# ---------------------------------------------------------------------------
# Operation entry / edit
# ---------------------------------------------------------------------------
class TransactionDialog(QDialog):
    """Create or edit one operation of the current user."""

    KINDS = (InvestmentKind.buy, InvestmentKind.sell, InvestmentKind.dividend,
             InvestmentKind.split)

    def __init__(
        self,
        session: Session,
        user_id: int,
        provider: MarketDataProvider,
        theme: dict,
        transaction: Optional[InvestmentTransaction] = None,
        default_security_id: Optional[int] = None,
        default_kind: InvestmentKind = InvestmentKind.buy,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._user_id = user_id
        self._provider = provider
        self._t = theme
        self._editing = transaction
        self._runner = BackgroundRunner(self)
        self._amount_auto = transaction is None
        self._setting_amount = False
        # (symbol, trade date) → (close date, close, fx rate) fetched from the provider
        self._market: Dict[Tuple[str, date], Tuple[Optional[date], Optional[float], Optional[float]]] = {}
        self._pending_lookup: Optional[Tuple[str, date]] = None
        self._suggested_close: Optional[float] = None
        self._held: Optional[float] = None
        self.saved: Optional[InvestmentTransaction] = None
        self.created_security = False

        self.setWindowTitle("Editar operación" if transaction else "Nueva operación")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._build()
        self.setStyleSheet(dialog_stylesheet(theme))
        self._load(transaction, default_security_id, default_kind)

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        t = self._t
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)

        title = QLabel(self.windowTitle())
        title.setObjectName("dlgTitle")
        layout.addWidget(title)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._form = form

        self._cmb_kind = QComboBox()
        for kind in self.KINDS:
            self._cmb_kind.addItem(KIND_LABELS[kind], kind)
        self._cmb_kind.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow("Tipo:", self._cmb_kind)

        security_row = QHBoxLayout()
        self._cmb_security = QComboBox()
        self._cmb_security.setMinimumWidth(300)
        self._cmb_security.currentIndexChanged.connect(self._on_security_changed)
        security_row.addWidget(self._cmb_security, 1)
        btn_search = QPushButton(" Buscar…")
        btn_search.setObjectName("btnSecondary")
        btn_search.setIcon(qta.icon("mdi.magnify", color=t["text_secondary"]))
        btn_search.setToolTip("Buscar un valor nuevo en Yahoo Finance")
        btn_search.clicked.connect(self._open_search)
        security_row.addWidget(btn_search)
        form.addRow("Valor:", security_row)

        self._lbl_security = QLabel("")
        self._lbl_security.setObjectName("dlgHint")
        form.addRow("", self._lbl_security)

        self._date = QDateEdit()
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat("dd/MM/yyyy")
        self._date.setMaximumDate(_qdate(date.today()))
        self._date.dateChanged.connect(self._on_inputs_changed)
        form.addRow("Fecha:", self._date)

        qty_row = QHBoxLayout()
        self._qty = _spin(6, trim=True)
        self._qty.valueChanged.connect(self._on_inputs_changed)
        qty_row.addWidget(self._qty)
        self._lbl_held = QLabel("")
        self._lbl_held.setObjectName("dlgHint")
        qty_row.addWidget(self._lbl_held)
        self._btn_all = QPushButton("Vender todas")
        self._btn_all.setObjectName("btnLink")
        self._btn_all.clicked.connect(self._sell_all)
        qty_row.addWidget(self._btn_all)
        qty_row.addStretch()
        form.addRow("Nº de acciones:", qty_row)

        price_row = QHBoxLayout()
        self._price = _spin(4, trim=True)
        self._price.valueChanged.connect(self._on_inputs_changed)
        price_row.addWidget(self._price)
        self._lbl_close = QLabel("")
        self._lbl_close.setObjectName("dlgHint")
        self._lbl_close.setMinimumWidth(0)
        price_row.addWidget(self._lbl_close)
        self._btn_use_close = QPushButton("Usar")
        self._btn_use_close.setObjectName("btnLink")
        self._btn_use_close.clicked.connect(self._use_close)
        price_row.addWidget(self._btn_use_close)
        price_row.addStretch()
        form.addRow("Precio por acción:", price_row)

        self._fees = _spin(2, " €")
        self._fees.valueChanged.connect(self._on_inputs_changed)
        form.addRow("Comisiones:", self._fees)

        self._ratio = _spin(4, maximum=1e6, trim=True)
        form.addRow("Acciones nuevas por\ncada acción antigua:", self._ratio)

        amount_row = QHBoxLayout()
        self._amount = _spin(2, " €")
        self._amount.valueChanged.connect(self._on_amount_edited)
        amount_row.addWidget(self._amount)
        self._btn_recalc = QPushButton("Recalcular")
        self._btn_recalc.setObjectName("btnLink")
        self._btn_recalc.setToolTip("Volver al importe estimado")
        self._btn_recalc.clicked.connect(self._recalculate)
        amount_row.addWidget(self._btn_recalc)
        amount_row.addStretch()
        self._lbl_amount = QLabel("Importe total (EUR):")
        form.addRow(self._lbl_amount, amount_row)

        self._lbl_estimate = QLabel("")
        self._lbl_estimate.setObjectName("dlgHint")
        self._lbl_estimate.setWordWrap(True)
        form.addRow("", self._lbl_estimate)

        self._withheld = _spin(2, " €")
        form.addRow("Retención en origen:", self._withheld)

        self._cash_paid = _spin(2, " €")
        form.addRow("Pagado de tu bolsillo:", self._cash_paid)

        self._cmb_broker = QComboBox()
        self._cmb_broker.setEditable(True)
        self._cmb_broker.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form.addRow("Bróker:", self._cmb_broker)

        self._notes = QLineEdit()
        self._notes.setPlaceholderText("Opcional")
        form.addRow("Notas:", self._notes)
        layout.addLayout(form)

        self._lbl_error = QLabel("")
        self._lbl_error.setObjectName("dlgError")
        self._lbl_error.setWordWrap(True)
        self._lbl_error.setVisible(False)
        layout.addWidget(self._lbl_error)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.setObjectName("btnSecondary")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._btn_save = QPushButton("Guardar")
        self._btn_save.setObjectName("btnPrimary")
        self._btn_save.setDefault(True)
        self._btn_save.clicked.connect(self._save)
        buttons.addWidget(self._btn_save)
        layout.addLayout(buttons)

    def _load(self, txn, default_security_id, default_kind) -> None:
        if txn is not None and InvestmentKind(txn.kind) == InvestmentKind.grant:
            self._cmb_kind.addItem(KIND_LABELS[InvestmentKind.grant], InvestmentKind.grant)
        self._reload_securities(txn.security_id if txn else default_security_id)
        for broker in known_brokers(self._session, self._user_id):
            self._cmb_broker.addItem(broker)

        if txn is None:
            self._select_kind(default_kind)
            self._date.setDate(_qdate(date.today()))
            self._cmb_broker.setCurrentIndex(0)
            self._on_kind_changed()
            return

        self._select_kind(InvestmentKind(txn.kind))
        self._date.setDate(_qdate(txn.trade_date))
        self._qty.setValue(txn.quantity or 0.0)
        self._price.setValue(txn.price or 0.0)
        self._fees.setValue(txn.fees_eur or 0.0)
        self._ratio.setValue(txn.split_ratio or 0.0)
        self._withheld.setValue(txn.tax_withheld_eur or 0.0)
        self._cash_paid.setValue(txn.cash_paid_eur or 0.0)
        self._set_amount(txn.amount_eur or 0.0)
        self._cmb_broker.setCurrentText(txn.broker)
        self._notes.setText(txn.notes or "")
        self._on_kind_changed()

    def _select_kind(self, kind: InvestmentKind) -> None:
        index = self._cmb_kind.findData(kind)
        self._cmb_kind.setCurrentIndex(max(index, 0))

    def _reload_securities(self, select_id: Optional[int]) -> None:
        self._cmb_security.blockSignals(True)
        self._cmb_security.clear()
        self._cmb_security.addItem("Selecciona un valor…", None)
        for security in picker_securities(self._session, self._user_id):
            self._cmb_security.addItem(f"{security.name}  ·  {security.symbol}", security.id)
        index = self._cmb_security.findData(select_id) if select_id else 0
        self._cmb_security.setCurrentIndex(max(index, 0))
        self._cmb_security.blockSignals(False)
        self._on_security_changed()

    # -- state helpers ---------------------------------------------------
    def _kind(self) -> InvestmentKind:
        return self._cmb_kind.currentData()

    def _security(self) -> Optional[Security]:
        security_id = self._cmb_security.currentData()
        return self._session.get(Security, security_id) if security_id else None

    def _trade_date(self) -> date:
        return _pydate(self._date.date())

    def _set_amount(self, value: float) -> None:
        self._setting_amount = True
        self._amount.setValue(value)
        self._setting_amount = False

    def _show_error(self, text: str) -> None:
        self._lbl_error.setText(text)
        self._lbl_error.setVisible(bool(text))

    # -- reactions ---------------------------------------------------------
    def _on_kind_changed(self, *_args) -> None:
        kind = self._kind()
        trade = kind in (InvestmentKind.buy, InvestmentKind.sell, InvestmentKind.grant)
        rows = {
            self._qty: trade, self._price: trade, self._fees: trade,
            self._ratio: kind == InvestmentKind.split,
            self._amount: kind != InvestmentKind.split,
            self._lbl_estimate: trade,
            self._withheld: kind == InvestmentKind.dividend,
            self._cash_paid: kind == InvestmentKind.grant,
        }
        for widget, visible in rows.items():
            self._set_row_visible(widget, visible)
        self._lbl_amount.setText({
            InvestmentKind.buy: "Importe total cargado (EUR):",
            InvestmentKind.sell: "Importe neto recibido (EUR):",
            InvestmentKind.dividend: "Dividendo neto recibido (EUR):",
            InvestmentKind.grant: "Valor fiscal al recibirlas (EUR):",
        }.get(kind, "Importe (EUR):"))
        self._on_inputs_changed()

    def _set_row_visible(self, widget: QWidget, visible: bool) -> None:
        for row in range(self._form.rowCount()):
            field = self._form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if field is None:
                continue
            item_widget = field.widget()
            item_layout = field.layout()
            contains = item_widget is widget or (
                item_layout is not None and any(
                    item_layout.itemAt(i).widget() is widget
                    for i in range(item_layout.count())
                )
            )
            if contains:
                self._form.setRowVisible(row, visible)
                return

    def _on_security_changed(self, *_args) -> None:
        security = self._security()
        if security is None:
            self._lbl_security.setText("")
            self._price.setSuffix("")
        else:
            parts = [ASSET_LABELS.get(AssetType(security.asset_type), ""),
                     security.exchange or "", f"cotiza en {security.currency}"]
            if security.isin:
                parts.append(f"ISIN {security.isin}")
            self._lbl_security.setText("  ·  ".join(p for p in parts if p))
            self._price.setSuffix(f" {security.currency}")
            if self._editing is None:
                last_broker = self._last_broker(security.id)
                if last_broker:
                    self._cmb_broker.setCurrentText(last_broker)
        self._on_inputs_changed()

    def _last_broker(self, security_id: int) -> Optional[str]:
        row = (self._session.query(InvestmentTransaction.broker)
               .filter(InvestmentTransaction.user_id == self._user_id,
                       InvestmentTransaction.security_id == security_id)
               .order_by(InvestmentTransaction.trade_date.desc()).first())
        return row[0] if row else None

    def _on_amount_edited(self, *_args) -> None:
        if not self._setting_amount:
            self._amount_auto = False
            self._update_estimate_label()

    def _recalculate(self) -> None:
        self._amount_auto = True
        self._on_inputs_changed()

    def _on_inputs_changed(self, *_args) -> None:
        self._show_error("")
        self._update_held()
        self._update_market_hints()
        self._update_estimate_label()

    # -- held shares (sales) ---------------------------------------------
    def _update_held(self) -> None:
        security = self._security()
        is_sell = self._kind() == InvestmentKind.sell
        self._held = None
        if security is not None and is_sell:
            day = self._trade_date()
            rows = [t for t in (self._session.query(InvestmentTransaction)
                                .filter(InvestmentTransaction.user_id == self._user_id,
                                        InvestmentTransaction.security_id == security.id,
                                        InvestmentTransaction.trade_date <= day))
                    if self._editing is None or t.id != self._editing.id]
            try:
                position = build_positions(rows).get(security.id)
                self._held = position.quantity if position else 0.0
            except LedgerError:
                self._held = None
        self._lbl_held.setText(
            f"Tienes {fmt_quantity(self._held)} el {fmt_date(self._trade_date())}"
            if self._held is not None else "")
        self._lbl_held.setVisible(self._held is not None)
        self._btn_all.setVisible(bool(self._held))

    def _sell_all(self) -> None:
        if self._held:
            self._qty.setValue(self._held)

    # -- market data (close of the day, FX) ------------------------------
    def _market_for(self, security: Security, day: date):
        """(close date, close, fx) from the cache, or the provider's answer."""
        fetched = self._market.get((security.symbol, day))
        if fetched is not None:
            return fetched
        cached = close_on(self._session, security.id, day)
        rate = fx_rate(self._session, security.currency, on=day)
        pair = fx_pair(security.currency)
        fx_fresh = pair is None or rate is not None
        if cached is not None and day - cached.price_date <= _MAX_CACHE_GAP and fx_fresh:
            return cached.price_date, cached.close, rate
        return None

    def _update_market_hints(self) -> None:
        security = self._security()
        kind = self._kind()
        self._suggested_close = None
        if security is None or kind not in (InvestmentKind.buy, InvestmentKind.sell,
                                            InvestmentKind.grant):
            self._lbl_close.setText("")
            self._btn_use_close.setVisible(False)
            return
        day = self._trade_date()
        market = self._market_for(security, day)
        if market is None:
            self._lbl_close.setText("Consultando cotización…")
            self._btn_use_close.setVisible(False)
            self._lookup(security, day)
            return
        close_date, close, _rate = market
        if close is None:
            self._lbl_close.setText("Sin cotización ese día")
            self._btn_use_close.setVisible(False)
            return
        self._suggested_close = close
        self._lbl_close.setText(
            f"Cierre del {fmt_date(close_date)}: {fmt_money(close, security.currency, 2)}")
        self._btn_use_close.setVisible(True)

    def _lookup(self, security: Security, day: date) -> None:
        key = (security.symbol, day)
        if self._pending_lookup == key:
            return
        self._pending_lookup = key
        symbols = [security.symbol]
        pair = fx_pair(security.currency)
        if pair:
            symbols.append(fx_symbol(pair[0]))
        provider = self._provider
        start = day - timedelta(days=10)

        def fetch():
            return provider.daily_closes(symbols, start=start, end=day)

        def done(closes, error):
            self._pending_lookup = None
            if error is not None:
                self._market[key] = (None, None, None)
            else:
                rows = [r for r in closes.get(security.symbol, []) if r[0] <= day]
                close_date, close = rows[-1] if rows else (None, None)
                rate = 1.0
                if pair:
                    fx_rows = [r for r in closes.get(fx_symbol(pair[0]), []) if r[0] <= day]
                    rate = fx_rows[-1][1] * pair[1] if fx_rows else None
                self._market[key] = (close_date, close, rate)
            current = self._security()
            if current is not None and (current.symbol, self._trade_date()) == key:
                self._update_market_hints()
                self._update_estimate_label()

        self._runner.run(fetch, done, latest_only=True)

    def _use_close(self) -> None:
        if self._suggested_close is not None:
            self._price.setValue(self._suggested_close)

    # -- EUR estimate ----------------------------------------------------
    def _current_fx(self, security: Security) -> Optional[float]:
        market = self._market_for(security, self._trade_date())
        if market is not None and market[2] is not None:
            return market[2]
        return fx_rate(self._session, security.currency, on=self._trade_date())

    def _update_estimate_label(self) -> None:
        security = self._security()
        kind = self._kind()
        self._btn_recalc.setVisible(not self._amount_auto and kind in (
            InvestmentKind.buy, InvestmentKind.sell))
        if security is None or kind not in (InvestmentKind.buy, InvestmentKind.sell,
                                            InvestmentKind.grant):
            self._lbl_estimate.setText("")
            return
        fx = self._current_fx(security)
        estimate = estimate_amount_eur(kind, self._qty.value(), self._price.value(),
                                       self._fees.value(), fx)
        if self._amount_auto and estimate is not None and kind != InvestmentKind.grant:
            self._set_amount(estimate)
        if security.currency == "EUR":
            text = "Acciones × precio ± comisiones."
        elif fx:
            per_eur = fx / (fx_pair(security.currency) or ("", 1.0))[1]
            text = (f"Estimado con 1 € = {fmt_number(per_eur, 4)} "
                    f"{fx_pair(security.currency)[0]}.")
        else:
            text = "Sin tipo de cambio para esa fecha: escribe el importe del extracto."
        if security.currency != "EUR":
            text += " Corrígelo con el importe exacto en EUR del extracto del bróker."
        self._lbl_estimate.setText(text)

    # -- search / save ---------------------------------------------------
    def _open_search(self) -> None:
        dialog = SecuritySearchDialog(self._session, self._provider, self._t, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.security is not None:
            self.created_security = True
            self._reload_securities(dialog.security.id)

    def draft(self) -> TransactionDraft:
        security = self._security()
        kind = self._kind()
        return TransactionDraft(
            id=self._editing.id if self._editing else None,
            security_id=security.id if security else 0,
            kind=kind,
            trade_date=self._trade_date(),
            broker=self._cmb_broker.currentText(),
            quantity=self._qty.value(),
            price=self._price.value() or None,
            currency=security.currency if security else None,
            amount_eur=self._amount.value(),
            fees_eur=self._fees.value(),
            tax_withheld_eur=self._withheld.value(),
            cash_paid_eur=self._cash_paid.value() if kind == InvestmentKind.grant else None,
            split_ratio=self._ratio.value() or None,
            notes=self._notes.text(),
        )

    def _save(self) -> None:
        if self._security() is None:
            self._show_error("Selecciona un valor (o búscalo con «Buscar…»).")
            return
        try:
            self.saved = save_transaction(self._session, self._user_id, self.draft())
        except LedgerError as exc:
            self._show_error(str(exc))
            return
        self.accept()

    def done(self, result: int) -> None:
        self._runner.shutdown()
        super().done(result)

