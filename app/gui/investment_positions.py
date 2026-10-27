"""
app/gui/investment_positions.py
===============================
FAMILYFIN — "Posiciones" tab of the Inversiones view.

  KPI cards   Valor de mercado · Capital invertido · Plusvalía latente ·
              Variación del día · Realizado <año> · Dividendos <año> ·
              Rentabilidad anual (XIRR)
  banner      securities without a usable price (valued at cost)
  table       one row per open position (closed ones on request)

Everything comes from investments.snapshot.portfolio_snapshot(), which only
reads the local cache; the view reloads this panel after each price refresh.
Double-clicking a row emits `security_activated(symbol)` so the view can
jump to that security's operations.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.db.models import InvestmentTransaction
from app.gui._format import fmt_date, fmt_eur, fmt_money, fmt_pct, fmt_quantity
from app.gui.dashboard import _KPICard
from app.investments.snapshot import PortfolioSnapshot, PositionRow, portfolio_snapshot

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
SYMBOL_ROLE = Qt.ItemDataRole.UserRole + 2

COLUMNS = ["Valor", "Acciones", "Coste medio (EUR)", "Cotización", "Día",
           "Valor (EUR)", "Peso", "Plusvalía (EUR)", "Plusvalía %", "Bróker"]

# XIRR over a few days extrapolates wildly; below this span it is not shown.
MIN_XIRR_DAYS = 30


class _MetricCard(_KPICard):
    """Dashboard KPI card whose main value can take a gain/loss colour."""

    def set_metric(self, value: str, sub: str = "", badge: str = "",
                   badge_positive: bool = True, value_color: Optional[str] = None) -> None:
        self.set_value(value, sub, badge, badge_positive)
        self._lbl_value.setStyleSheet(f"color: {value_color};" if value_color else "")


class PositionsPanel(QWidget):
    security_activated = Signal(str)

    def __init__(self, session: Session, user_id: int, theme: dict) -> None:
        super().__init__()
        self._session = session
        self._user_id = user_id
        self._t = theme
        self.snapshot: Optional[PortfolioSnapshot] = None
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 0)
        root.setSpacing(12)

        t = self._t
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        specs = [
            ("value", "Valor de mercado", "mdi.briefcase-outline", t["investment"]),
            ("cost", "Capital invertido", "mdi.cash-plus", t["accent"]),
            ("gain", "Plusvalía latente", "mdi.chart-line", t["income"]),
            ("day", "Variación del día", "mdi.calendar-today", t["accent"]),
            ("realized", "Realizado", "mdi.check-circle-outline", t["income"]),
            ("dividends", "Dividendos", "mdi.cash-multiple", t["income"]),
            ("xirr", "Rentabilidad anual", "mdi.percent-outline", t["investment"]),
        ]
        self._cards = {}
        for index, (key, label, icon, color) in enumerate(specs):
            card = _MetricCard(label, icon, color, t)
            card.setMinimumHeight(104)
            self._cards[key] = card
            grid.addWidget(card, index // 4, index % 4)
        root.addLayout(grid)

        self._banner = QLabel("")
        self._banner.setObjectName("invBanner")
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        bar = QHBoxLayout()
        title = QLabel("Posiciones")
        title.setObjectName("sectionTitle")
        bar.addWidget(title)
        bar.addStretch()
        self._chk_closed = QCheckBox("Mostrar posiciones cerradas")
        self._chk_closed.toggled.connect(self._fill_table)
        bar.addWidget(self._chk_closed)
        self._cmb_broker = QComboBox()
        self._cmb_broker.setObjectName("filterCombo")
        self._cmb_broker.setFixedHeight(32)
        self._cmb_broker.setMinimumWidth(160)
        self._cmb_broker.currentIndexChanged.connect(self.reload)
        bar.addWidget(self._cmb_broker)
        root.addLayout(bar)

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
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setToolTip("Doble clic para ver las operaciones de este valor")
        header = self._table.horizontalHeader()
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # Default order: largest position first (Qt would pick column 0).
        self._table.sortByColumn(5, Qt.SortOrder.DescendingOrder)
        root.addWidget(self._table, 1)

    # ------------------------------------------------------------------
    def reload(self, *_args) -> None:
        self._load_brokers()
        self.snapshot = portfolio_snapshot(self._session, self._user_id,
                                           broker=self._cmb_broker.currentData())
        self._fill_cards()
        self._fill_banner()
        self._fill_table()

    def _load_brokers(self) -> None:
        current = self._cmb_broker.currentData()
        brokers = sorted(b for (b,) in self._session.query(InvestmentTransaction.broker)
                         .filter(InvestmentTransaction.user_id == self._user_id).distinct())
        self._cmb_broker.blockSignals(True)
        self._cmb_broker.clear()
        self._cmb_broker.addItem("Todos los brókers", None)
        for broker in brokers:
            self._cmb_broker.addItem(broker, broker)
        index = self._cmb_broker.findData(current) if current else 0
        self._cmb_broker.setCurrentIndex(max(index, 0))
        self._cmb_broker.setVisible(len(brokers) > 1)
        self._cmb_broker.blockSignals(False)

    def _gain_color(self, value: Optional[float]) -> Optional[str]:
        if value is None or abs(value) < 0.005:
            return None
        return self._t["income"] if value > 0 else self._t["expense"]

    def _fill_cards(self) -> None:
        snap = self.snapshot
        tot = snap.totals
        year = snap.as_of.year
        n = tot.open_positions
        prices = (f" · precios del {fmt_date(tot.last_price_date)}"
                  if tot.last_price_date else "")
        c = self._cards
        positions = (f"{n} posición abierta" if n == 1 else f"{n} posiciones abiertas") + prices
        c["value"]._lbl_label.setText("Valor total" if tot.cash else "Valor de mercado")
        if tot.cash:
            # Whole account, like DEGIRO's «Cuenta completa»: securities + cash.
            c["value"].set_metric(fmt_eur(tot.total_value_eur),
                                  f"Valores {fmt_eur(tot.market_value_eur)} + efectivo "
                                  f"{fmt_eur(tot.cash_eur)} · {positions}")
            c["value"].setToolTip("Efectivo sin invertir, según el último extracto importado: " + ", ".join(
                f"{line.broker} {fmt_money(line.amount, line.currency, 2)} ({fmt_date(line.as_of)})"
                for line in tot.cash))
        else:
            c["value"].set_metric(fmt_eur(tot.market_value_eur), positions)
            c["value"].setToolTip("")
        c["cost"].set_metric(fmt_eur(tot.cost_basis_eur), "Coste FIFO de lo que tienes")
        c["gain"].set_metric(fmt_eur(tot.unrealized_gain_eur, signed=True),
                             "Si vendieras hoy, antes de impuestos",
                             fmt_pct(tot.unrealized_pct) if tot.unrealized_pct is not None else "",
                             tot.unrealized_gain_eur >= 0,
                             self._gain_color(tot.unrealized_gain_eur))
        c["day"].set_metric(fmt_eur(tot.day_change_eur, signed=True),
                            "Último cierre frente al anterior",
                            fmt_pct(tot.day_change_pct) if tot.day_change_pct is not None else "",
                            tot.day_change_eur >= 0,
                            self._gain_color(tot.day_change_eur))
        c["realized"]._lbl_label.setText(f"Realizado {year}")
        c["realized"].set_metric(fmt_eur(tot.realized_ytd_eur, signed=True),
                                 f"Ventas FIFO · histórico {fmt_eur(tot.realized_total_eur, signed=True)}",
                                 value_color=self._gain_color(tot.realized_ytd_eur))
        c["dividends"]._lbl_label.setText(f"Dividendos {year}")
        c["dividends"].set_metric(fmt_eur(tot.dividends_ytd_eur),
                                  f"Netos · histórico {fmt_eur(tot.dividends_total_eur)}")
        first = self._first_trade_date()
        if tot.xirr is not None and first and (snap.as_of - first).days >= MIN_XIRR_DAYS:
            c["xirr"].set_metric(fmt_pct(tot.xirr), "Anualizada según cuándo invertiste cada euro",
                                 value_color=self._gain_color(tot.xirr))
        else:
            c["xirr"].set_metric("—", "Hace falta al menos un mes de historial")

    def _first_trade_date(self):
        from sqlalchemy import func
        return (self._session.query(func.min(InvestmentTransaction.trade_date))
                .filter(InvestmentTransaction.user_id == self._user_id).scalar())

    def _fill_banner(self) -> None:
        missing = self.snapshot.totals.missing_prices
        stale = [r.security.symbol for r in self.snapshot.open_rows if r.stale]
        parts = []
        if missing:
            parts.append(f"Sin cotización todavía para {', '.join(missing)}: se valoran a su coste "
                         "hasta la próxima actualización.")
        if stale:
            parts.append(f"Cotización con más de una semana de antigüedad: {', '.join(stale)}.")
        self._banner.setText(" ".join(parts))
        self._banner.setVisible(bool(parts))

    # ------------------------------------------------------------------
    def _fill_table(self, *_args) -> None:
        if self.snapshot is None:
            return
        rows = self.snapshot.rows if self._chk_closed.isChecked() else self.snapshot.open_rows
        sort_column, sort_order = self._proxy.sortColumn(), self._proxy.sortOrder()
        self._table.setSortingEnabled(False)
        self._model.removeRows(0, self._model.rowCount())
        for row in rows:
            self._model.appendRow(self._make_row(row))
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(sort_column, sort_order)

    def _make_row(self, row: PositionRow) -> List[QStandardItem]:
        t = self._t
        security = row.security

        def cell(text, sort=None, right=True, color=None, tooltip=None, bold=False):
            item = QStandardItem(text)
            item.setData(sort if sort is not None else text.lower(), SORT_ROLE)
            item.setData(security.symbol, SYMBOL_ROLE)
            if right:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if color:
                item.setForeground(QColor(color))
            if tooltip:
                item.setToolTip(tooltip)
            if bold:
                font = QFont()
                font.setBold(True)
                item.setFont(font)
            return item

        name = cell(f"{security.name}  ·  {security.symbol}", right=False,
                    tooltip=" · ".join(p for p in (security.exchange, security.currency,
                                                   security.isin) if p))
        if not row.is_open:
            name.setForeground(QColor(t["text_secondary"]))

        if row.price is None:
            price_text, price_tip, price_color = "—", "Sin cotización guardada", t["warning"]
        else:
            price_text = fmt_money(row.price, security.currency, 2)
            price_tip = f"Cierre del {fmt_date(row.price_date)}"
            price_color = t["warning"] if (row.stale or not row.has_price) else None
            if row.stale:
                price_tip += " (antigua)"

        if row.is_open:
            gain_cell = cell(fmt_eur(row.unrealized_gain_eur, signed=True) if row.has_price else "—",
                             sort=row.unrealized_gain_eur,
                             color=self._gain_color(row.unrealized_gain_eur), bold=True,
                             tooltip=(f"Realizado: {fmt_eur(row.realized_gain_eur, signed=True)} · "
                                      f"Dividendos: {fmt_eur(row.dividends_eur)}"))
            pct_cell = cell(fmt_pct(row.unrealized_pct), sort=row.unrealized_pct or 0.0,
                            color=self._gain_color(row.unrealized_pct))
        else:
            gain_cell = cell(fmt_eur(row.total_return_eur, signed=True), sort=row.total_return_eur,
                             color=self._gain_color(row.total_return_eur), bold=True,
                             tooltip="Posición cerrada: realizado + dividendos")
            pct_cell = cell("cerrada", sort=-1e9, color=t["text_secondary"])

        return [
            name,
            cell(fmt_quantity(row.quantity), sort=row.quantity),
            cell(fmt_eur(row.average_cost_eur) if row.average_cost_eur is not None else "—",
                 sort=row.average_cost_eur or 0.0),
            cell(price_text, sort=row.price or 0.0, color=price_color, tooltip=price_tip),
            cell(fmt_pct(row.day_change_pct), sort=row.day_change_pct or 0.0,
                 color=self._gain_color(row.day_change_pct),
                 tooltip=fmt_eur(row.day_change_eur, signed=True)
                 if row.day_change_eur is not None else None),
            cell(fmt_eur(row.market_value_eur) if row.is_open else "—",
                 sort=row.market_value_eur, bold=True,
                 color=t["warning"] if not row.has_price else None,
                 tooltip=None if row.has_price else "Valorada a coste: falta la cotización"),
            cell(fmt_pct(row.weight, signed=False) if row.is_open else "—", sort=row.weight),
            gain_cell,
            pct_cell,
            cell(", ".join(row.brokers), right=False),
        ]

    def _on_double_click(self, index) -> None:
        symbol = self._proxy.data(index, SYMBOL_ROLE)
        if symbol:
            self.security_activated.emit(symbol)

    # ------------------------------------------------------------------
    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        for card in self._cards.values():
            card.apply_style(theme)
        t = theme
        self._banner.setStyleSheet(
            f"QLabel#invBanner {{ background-color: {t['bg_secondary']}; color: {t['warning']};"
            f" border: 1px solid {t['warning']}; border-radius: 8px; padding: 8px 12px;"
            f" font-size: 12px; }}")
        self.setStyleSheet(f"""
            QLabel#sectionTitle {{ color: {t["text_primary"]}; font-size: 14px; font-weight: 700; }}
            QCheckBox {{ color: {t["text_secondary"]}; font-size: 12px; background: transparent; }}
        """)
        if self.snapshot is not None:
            self._fill_cards()
            self._fill_table()
