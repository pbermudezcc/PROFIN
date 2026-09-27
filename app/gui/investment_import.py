"""
app/gui/investment_import.py
============================
FAMILYFIN — "Importar extracto" dialog of the Inversiones view (DEGIRO).

  1. Choose the "Estado de cuenta" (.xlsx / .csv). It is parsed locally.
  2. Each security's Yahoo symbol is looked up in the background; the
     proposal and its alternatives (with how well their price matches what
     was paid) appear in an editable combo — any symbol can be typed.
  3. Preview of every operation and dividend, marked "Nueva" or "Ya
     importada" (same DEGIRO order id / dividend key), plus what is ignored.
  4. "Importar": data of new securities is fetched in the background, then
     everything is written for the logged-in user in one transaction.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.gui._background import BackgroundRunner
from app.gui._format import fmt_date, fmt_eur, fmt_money, fmt_quantity
from app.gui.investment_dialogs import dialog_stylesheet
from app.investments.importers.degiro import ParsedStatement, StatementError, parse_statement
from app.investments.importers.service import (
    ImportSummary,
    already_imported,
    apply_import,
    catalogue_isins,
    fetch_security_infos,
    symbols_to_fetch,
)
from app.investments.importers.symbols import listing_currency, suggest_symbols
from app.investments.market_data import MarketDataError, MarketDataProvider
from app.investments.portfolio import LedgerError

_KIND = {"buy": "Compra", "sell": "Venta"}


def _item(text: str, right: bool = False, color: Optional[str] = None,
          tooltip: Optional[str] = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if color:
        item.setForeground(QColor(color))
    if tooltip:
        item.setToolTip(tooltip)
    return item


def _table(headers) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    header = table.horizontalHeader()
    for col in range(len(headers)):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    return table


class ImportStatementDialog(QDialog):

    def __init__(self, session: Session, user_id: int, username: str,
                 provider: MarketDataProvider, theme: dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session = session
        self._user_id = user_id
        self._provider = provider
        self._t = theme
        self._runner = BackgroundRunner(self)
        self.statement: Optional[ParsedStatement] = None
        self.summary: Optional[ImportSummary] = None
        self._combos: Dict[str, QComboBox] = {}
        self._known_refs: set = set()

        self.setWindowTitle("Importar extracto de DEGIRO")
        self.setModal(True)
        self.resize(1080, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title = QLabel(f"Importar extracto de DEGIRO · {username}")
        title.setObjectName("dlgTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Exporta en DEGIRO «Actividad → Estado de cuenta» (desde la apertura de la cuenta) como "
            "Excel o CSV. Las operaciones se añaden al usuario con el que has iniciado sesión; las "
            "que ya importaste antes se reconocen y no se duplican.")
        hint.setObjectName("dlgHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self._btn_file = QPushButton(" Elegir archivo…")
        self._btn_file.setObjectName("btnSecondary")
        self._btn_file.setIcon(qta.icon("mdi.file-excel-outline", color=theme["income"]))
        self._btn_file.clicked.connect(self._choose_file)
        row.addWidget(self._btn_file)
        self._lbl_file = QLabel("Ningún archivo seleccionado")
        self._lbl_file.setObjectName("dlgHint")
        row.addWidget(self._lbl_file, 1)
        layout.addLayout(row)

        self._lbl_securities = QLabel("Valores y su cotización en Yahoo Finance")
        self._lbl_securities.setObjectName("sectionTitle")
        layout.addWidget(self._lbl_securities)
        self._securities = _table(["ISIN", "Valor", "Cotización (Yahoo)", "Precio frente a lo pagado"])
        self._securities.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._securities.setColumnWidth(2, 260)
        self._securities.verticalHeader().setDefaultSectionSize(40)    # room for the combos
        layout.addWidget(self._securities, 2)

        self._lbl_ops = QLabel("Operaciones")
        self._lbl_ops.setObjectName("sectionTitle")
        layout.addWidget(self._lbl_ops)
        self._ops = _table(["Fecha", "Valor", "Tipo", "Acciones", "Precio", "Importe (EUR)",
                            "Comisiones / retención", "Estado"])
        layout.addWidget(self._ops, 3)

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
        self._btn_import = QPushButton("Importar")
        self._btn_import.setObjectName("btnPrimary")
        self._btn_import.setEnabled(False)
        self._btn_import.clicked.connect(self._import)
        buttons.addWidget(self._btn_import)
        layout.addLayout(buttons)
        self.setStyleSheet(dialog_stylesheet(theme) + f"""
            QLabel#sectionTitle {{ color: {theme["text_primary"]}; font-size: 13px; font-weight: 700; }}
        """)

    # ------------------------------------------------------------------
    def _set_status(self, text: str, error: bool = False) -> None:
        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(
            f"color: {self._t['expense']}; font-weight: 600;" if error else "")

    def _choose_file(self) -> None:
        start = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, "Estado de cuenta de DEGIRO", start, "Extracto DEGIRO (*.xlsx *.csv)")
        if path:
            self.load_file(path)

    def load_file(self, path) -> None:
        path = Path(path)
        try:
            statement = parse_statement(path)
        except (StatementError, OSError) as exc:
            self._set_status(str(exc), error=True)
            return
        self.statement = statement
        self._known_refs = already_imported(self._session, self._user_id, statement)
        self._lbl_file.setText(
            f"{path.name} · del {fmt_date(statement.first_day)} al {fmt_date(statement.last_day)}")
        self._fill_operations()
        self._fill_securities({})
        self._set_status("Buscando la cotización de cada valor en Yahoo Finance…")
        self._btn_import.setEnabled(False)
        provider, products = self._provider, statement.products
        catalogue = catalogue_isins(self._session)
        self._runner.run(lambda: suggest_symbols(provider, products, catalogue),
                         self._on_symbols, latest_only=True)

    def _on_symbols(self, choices, error) -> None:
        if error is not None:
            self._set_status(f"No se pudo consultar Yahoo Finance ({error}). Escribe el símbolo "
                             "de cada valor a mano.", error=True)
            choices = {}
        self._fill_securities(choices)
        self._update_ready()

    # ------------------------------------------------------------------
    def _fill_securities(self, choices) -> None:
        products = self.statement.products
        self._securities.setRowCount(len(products))
        self._combos = {}
        for row, (isin, info) in enumerate(sorted(products.items(), key=lambda kv: kv[1]["name"])):
            choice = choices.get(isin)
            self._securities.setItem(row, 0, _item(isin))
            self._securities.setItem(row, 1, _item(info["name"]))
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            detail = "Buscando…"
            if choice is not None:
                for candidate in choice.candidates:
                    combo.addItem(f"{candidate.symbol}", candidate.symbol)
                    combo.setItemData(combo.count() - 1,
                                      f"{candidate.exchange} · {candidate.gap_text}",
                                      Qt.ItemDataRole.ToolTipRole)
                if choice.chosen and combo.findData(choice.chosen) < 0:
                    combo.insertItem(0, choice.chosen, choice.chosen)
                combo.setCurrentText(choice.chosen or "")
                if choice.from_catalogue:
                    detail = "Ya está en tu catálogo"
                else:
                    best = next((c for c in choice.candidates if c.symbol == choice.chosen), None)
                    detail = (f"{best.exchange} · {best.gap_text}" if best else
                              "Sin coincidencia: escribe el símbolo de Yahoo")
            combo.currentTextChanged.connect(lambda _t, i=isin: self._on_symbol_edited(i))
            self._combos[isin] = combo
            self._securities.setCellWidget(row, 2, combo)
            self._securities.setItem(row, 3, _item(detail))
        self._fit(self._securities)

    def _on_symbol_edited(self, isin: str) -> None:
        text = self._combos[isin].currentText().strip().upper()
        currency = self.statement.products[isin]["currency"]
        listing = listing_currency(text) if text else None
        for row in range(self._securities.rowCount()):
            if self._securities.item(row, 0).text() == isin:
                note = ("Cotiza en otra divisa que la compra" if listing and listing != currency
                        else "Elegido a mano")
                self._securities.setItem(row, 3, _item(note, color=self._t["warning"]
                                                       if "divisa" in note else None))
        self._update_ready()

    def _fill_operations(self) -> None:
        statement = self.statement
        t = self._t
        rows = ([("trade", x) for x in statement.trades]
                + [("dividend", x) for x in statement.dividends])
        rows.sort(key=lambda r: r[1].trade_date if r[0] == "trade" else r[1].pay_date)
        self._ops.setRowCount(len(rows))
        new = 0
        for row, (kind, x) in enumerate(rows):
            known = x.ref in self._known_refs
            new += not known
            state = _item("Ya importada" if known else "Nueva",
                          color=t["text_secondary"] if known else t["income"])
            if kind == "trade":
                cash = -x.amount_eur if x.kind == "buy" else x.amount_eur
                values = [
                    _item(fmt_date(x.trade_date)), _item(x.name),
                    _item(_KIND[x.kind] + (f" ({x.fills} ejec.)" if x.fills > 1 else "")),
                    _item(fmt_quantity(x.quantity), right=True),
                    _item(fmt_money(x.price, x.currency, 2), right=True),
                    _item(fmt_eur(cash, signed=True), right=True,
                          color=t["income"] if cash > 0 else t["expense"]),
                    _item(fmt_eur(x.fees_eur), right=True), state]
            else:
                values = [
                    _item(fmt_date(x.pay_date)), _item(x.name), _item("Dividendo"),
                    _item("—", right=True),
                    _item(fmt_money(x.gross, x.currency, 2) + " brutos", right=True),
                    _item(fmt_eur(x.net_eur, signed=True) if x.net_eur is not None else "se estimará",
                          right=True, color=t["income"]),
                    _item(fmt_eur(x.withheld_eur) if x.withheld_eur else "—", right=True), state]
            for col, item in enumerate(values):
                self._ops.setItem(row, col, item)
        self._fit(self._ops)
        ignored = ", ".join(f"{label} ({count})" for label, count in statement.ignored.items())
        self._lbl_ops.setText(
            f"Operaciones · {len(statement.trades)} compras/ventas y {len(statement.dividends)} "
            f"dividendos · {new} nuevas")
        self._ops.setToolTip(f"Se ignoran: {ignored}" if ignored else "")
        self._new = new

    @staticmethod
    def _fit(table: QTableWidget) -> None:
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def symbols(self) -> Dict[str, str]:
        return {isin: combo.currentText().strip().upper() for isin, combo in self._combos.items()}

    def _update_ready(self) -> None:
        missing = [isin for isin, symbol in self.symbols().items() if not symbol]
        warnings = list(self.statement.warnings) if self.statement else []
        if missing:
            text = f"Falta el símbolo de {len(missing)} valor(es)."
        elif not self._new:
            text = "Todas las operaciones de este extracto ya estaban importadas."
        else:
            text = f"Listo para importar {self._new} operaciones nuevas."
        ignored = self.statement.ignored if self.statement else {}
        if ignored:
            text += " Se ignoran: " + ", ".join(f"{k} ({v})" for k, v in ignored.items()) + "."
        if warnings:
            text += " Avisos: " + " ".join(warnings)
        self._set_status(text)
        self._btn_import.setEnabled(bool(self._new) and not missing and not self._runner.busy)

    # ------------------------------------------------------------------
    def _import(self) -> None:
        symbols = self.symbols()
        to_fetch = symbols_to_fetch(self._session, symbols.values())
        self._btn_import.setEnabled(False)
        self._set_status("Obteniendo los datos de los valores nuevos…")
        provider = self._provider
        self._runner.run(lambda: fetch_security_infos(provider, to_fetch),
                         lambda infos, error: self._finish_import(symbols, infos, error))

    def _finish_import(self, symbols, infos, error) -> None:
        if error is not None:
            message = str(error) if isinstance(error, MarketDataError) else f"Error: {error}"
            self._set_status(f"No se pudieron obtener los datos de algún valor: {message}", error=True)
            self._btn_import.setEnabled(True)
            return
        try:
            self.summary = apply_import(self._session, self._user_id, self.statement, symbols, infos)
        except LedgerError as exc:
            self._set_status(f"No se ha importado nada: {exc}", error=True)
            self._btn_import.setEnabled(True)
            return
        self.accept()

    def done(self, result: int) -> None:
        self._runner.shutdown()
        super().done(result)
