"""
app/gui/investment_tax.py
=========================
FAMILYFIN — "Fiscalidad" tab of the Inversiones view.

Year selector, summary cards (net result, losses deferred by the
anti-repurchase rule, computable result, gross dividends, withholding at
source), one table of FIFO sale lots and one of dividends, and an
"Exportar a Excel…" button. Data: investments.tax_report.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.gui._format import fmt_date, fmt_eur, fmt_quantity
from app.gui.investment_positions import _MetricCard
from app.investments.tax_report import TaxReport, export_xlsx, report_years, tax_report

SALE_COLUMNS = ["Valor", "Bróker", "Transmisión", "Adquisición", "Acciones",
                "Valor transmisión", "Valor adquisición", "Resultado", "No computable"]
DIVIDEND_COLUMNS = ["Valor", "Bróker", "Fecha de cobro", "Íntegro", "Retención origen", "Neto"]


def _table(columns: List[str]) -> tuple:
    model = QStandardItemModel(0, len(columns))
    model.setHorizontalHeaderLabels(columns)
    view = QTableView()
    view.setObjectName("invTable")
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    header = view.horizontalHeader()
    for col in range(len(columns)):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return model, view


def _fit_columns(view: QTableView) -> None:
    """Size columns to the rows just added, keeping the first one stretched.

    ResizeToContents sections do not re-measure rows appended to an unsorted
    model, and resizeColumnsToContents() drops the Stretch of column 0.
    """
    view.resizeColumnsToContents()
    view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)


class TaxPanel(QWidget):

    def __init__(self, session: Session, user_id: int, username: str, theme: dict) -> None:
        super().__init__()
        self._session = session
        self._user_id = user_id
        self._username = username
        self._t = theme
        self.report: Optional[TaxReport] = None
        self._build()

    def _build(self) -> None:
        t = self._t
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(12)

        bar = QHBoxLayout()
        label = QLabel("Ejercicio fiscal:")
        label.setObjectName("sectionTitle")
        bar.addWidget(label)
        self._cmb_year = QComboBox()
        self._cmb_year.setObjectName("filterCombo")
        self._cmb_year.setFixedHeight(32)
        self._cmb_year.setMinimumWidth(110)
        self._cmb_year.currentIndexChanged.connect(self._load_report)
        bar.addWidget(self._cmb_year)
        bar.addStretch()
        note = QLabel("Orientativo: compruébalo con el borrador de la renta (datos fiscales de la AEAT).")
        note.setObjectName("taxNote")
        bar.addWidget(note)
        self._btn_export = QPushButton(" Exportar a Excel…")
        self._btn_export.setObjectName("btnPrimary")
        self._btn_export.setFixedHeight(34)
        self._btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_export.clicked.connect(self._export)
        bar.addWidget(self._btn_export)
        root.addLayout(bar)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        specs = [
            ("net", "Resultado ventas", "mdi.scale-balance", t["accent"]),
            ("blocked", "No computable (antirrecompra)", "mdi.timer-sand", t["warning"]),
            ("computable", "Resultado computable", "mdi.check-decagram-outline", t["investment"]),
            ("dividends", "Dividendos íntegros", "mdi.cash-multiple", t["income"]),
            ("withheld", "Retenciones en origen", "mdi.bank-outline", t["neutral"]),
        ]
        self._cards = {}
        for key, text, icon, color in specs:
            card = _MetricCard(text, icon, color, t)
            card.setMinimumWidth(170)
            self._cards[key] = card
            cards.addWidget(card)
        root.addLayout(cards)

        self._lbl_sales = QLabel("Ganancias y pérdidas patrimoniales (FIFO, por lote)")
        self._lbl_sales.setObjectName("sectionTitle")
        root.addWidget(self._lbl_sales)
        self._sales_model, self._sales_view = _table(SALE_COLUMNS)
        root.addWidget(self._sales_view, 3)

        self._lbl_dividends = QLabel("Dividendos")
        self._lbl_dividends.setObjectName("sectionTitle")
        root.addWidget(self._lbl_dividends)
        self._div_model, self._div_view = _table(DIVIDEND_COLUMNS)
        root.addWidget(self._div_view, 2)

        self._lbl_empty = QLabel("No hay ventas ni dividendos registrados todavía.")
        self._lbl_empty.setObjectName("taxNote")
        self._lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._lbl_empty)

    # ------------------------------------------------------------------
    def reload(self) -> None:
        current = self._cmb_year.currentData()
        years = report_years(self._session, self._user_id)
        self._cmb_year.blockSignals(True)
        self._cmb_year.clear()
        for year in years:
            self._cmb_year.addItem(str(year), year)
        if not years:
            self._cmb_year.addItem(str(date.today().year), date.today().year)
        index = self._cmb_year.findData(current) if current else 0
        self._cmb_year.setCurrentIndex(max(index, 0))
        self._cmb_year.blockSignals(False)
        self._lbl_empty.setVisible(not years)
        self._load_report()

    def _gain_color(self, value: float) -> Optional[str]:
        if abs(value) < 0.005:
            return None
        return self._t["income"] if value > 0 else self._t["expense"]

    def _load_report(self, *_args) -> None:
        year = self._cmb_year.currentData()
        if year is None:
            return
        report = self.report = tax_report(self._session, self._user_id, year)
        c = self._cards
        c["net"].set_metric(fmt_eur(report.net_eur, signed=True),
                            f"Ganancias {fmt_eur(report.gains_eur)} · pérdidas {fmt_eur(report.losses_eur)}",
                            value_color=self._gain_color(report.net_eur))
        c["blocked"].set_metric(fmt_eur(report.blocked_losses_eur, signed=True),
                                "Pérdidas con recompra en el plazo",
                                value_color=self._t["warning"] if report.blocked_losses_eur else None)
        c["computable"].set_metric(fmt_eur(report.computable_net_eur, signed=True),
                                   "Lo que declararías este año",
                                   value_color=self._gain_color(report.computable_net_eur))
        c["dividends"].set_metric(fmt_eur(report.dividends_gross_eur),
                                  f"Netos cobrados {fmt_eur(report.dividends_net_eur)}")
        c["withheld"].set_metric(fmt_eur(report.dividends_withheld_eur),
                                 "Para la deducción por doble imposición")
        self._fill_sales(report)
        self._fill_dividends(report)
        self._btn_export.setEnabled(bool(report.sales or report.dividends))

    def _cell(self, text, right=False, color=None, tooltip=None) -> QStandardItem:
        item = QStandardItem(text)
        if right:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if color:
            item.setForeground(QColor(color))
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def _fill_sales(self, report: TaxReport) -> None:
        self._sales_model.removeRows(0, self._sales_model.rowCount())
        for s in report.sales:
            blocked_tip = None
            if s.blocked_eur:
                blocked_tip = ("Recompra de valores homogéneos en el plazo: "
                               + ", ".join(fmt_date(d) for d in s.repurchases)
                               + ". La pérdida se imputará al vender esas acciones.")
            self._sales_model.appendRow([
                self._cell(f"{s.security.name}  ·  {s.security.symbol}",
                           tooltip=f"ISIN {s.security.isin}" if s.security.isin else None),
                self._cell(s.broker),
                self._cell(fmt_date(s.sell_date)),
                self._cell(fmt_date(s.acquired)),
                self._cell(fmt_quantity(s.quantity), right=True),
                self._cell(fmt_eur(s.proceeds_eur), right=True),
                self._cell(fmt_eur(s.cost_eur), right=True),
                self._cell(fmt_eur(s.gain_eur, signed=True), right=True,
                           color=self._gain_color(s.gain_eur)),
                self._cell(fmt_eur(s.blocked_eur, signed=True) if s.blocked_eur else "—", right=True,
                           color=self._t["warning"] if s.blocked_eur else None, tooltip=blocked_tip),
            ])
        _fit_columns(self._sales_view)
        self._lbl_sales.setText(f"Ganancias y pérdidas patrimoniales (FIFO, por lote) · "
                                f"{len(report.sales)} línea{'s' if len(report.sales) != 1 else ''}")

    def _fill_dividends(self, report: TaxReport) -> None:
        self._div_model.removeRows(0, self._div_model.rowCount())
        for d in report.dividends:
            self._div_model.appendRow([
                self._cell(f"{d.security.name}  ·  {d.security.symbol}"),
                self._cell(d.broker),
                self._cell(fmt_date(d.pay_date)),
                self._cell(fmt_eur(d.gross_eur), right=True),
                self._cell(fmt_eur(d.withheld_eur) if d.withheld_eur else "—", right=True),
                self._cell(fmt_eur(d.net_eur), right=True, color=self._t["income"]),
            ])
        _fit_columns(self._div_view)
        self._lbl_dividends.setText(f"Dividendos · {len(report.dividends)}")

    def _export(self) -> None:
        if self.report is None:
            return
        default = Path.home() / "Documents" / f"Informe_fiscal_inversiones_{self.report.year}_{self._username}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Exportar informe fiscal", str(default),
                                              "Libro de Excel (*.xlsx)")
        if not path:
            return
        try:
            export_xlsx(self.report, Path(path), owner=self._username)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo exportar",
                                f"No se pudo guardar el archivo (¿está abierto en Excel?).\n{exc}")
            return
        QMessageBox.information(self, "Informe exportado", f"Informe guardado en:\n{path}")

    # ------------------------------------------------------------------
    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        for card in self._cards.values():
            card.apply_style(theme)
        self.setStyleSheet(f"""
            QLabel#sectionTitle {{ color: {theme["text_primary"]}; font-size: 14px; font-weight: 700; }}
            QLabel#taxNote {{ color: {theme["text_secondary"]}; font-size: 12px; }}
        """)
        if self.report is not None:
            self._load_report()
