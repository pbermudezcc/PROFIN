"""
app/gui/investment_import.py
============================
FAMILYFIN — "Importar extracto" dialog of the Inversiones view.

Supported files (detected from their header, see importers.service.parse_any):
  * DEGIRO "Estado de cuenta" (.xlsx / .csv): exact operations;
  * MyInvestor account movements (.csv / .xlsx): amounts only — the kind of
    each movement is guessed and quantities are estimated from the day's
    price, both editable here.

  1. Choose the file. It is parsed locally.
  2. Each product's Yahoo symbol is looked up in the background; the
     proposal and its alternatives appear in an editable combo.
  3. When quantities are missing, they are estimated in the background from
     the chosen symbols' closes (again after a symbol is changed).
  4. Preview of every movement, marked "Nueva" or "Ya importada".
  5. "Importar": data of new securities is fetched in the background, then
     everything is written for the logged-in user in one transaction.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import qtawesome as qta
from PySide6.QtCore import Qt, QTimer
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
from app.investments.importers.base import KIND_BUY, KIND_DIVIDEND, KIND_IGNORE, KIND_SELL
from app.investments.importers.degiro import StatementError
from app.investments.importers.service import (
    ImportSummary,
    already_imported,
    apply_import,
    catalogue_isins,
    estimate_quantities,
    fetch_closes,
    fetch_security_infos,
    parse_any,
    symbols_to_fetch,
)
from app.investments.importers.symbols import listing_currency, suggest_symbols
from app.investments.market_data import MarketDataError, MarketDataProvider
from app.investments.portfolio import LedgerError

KIND_LABELS = {KIND_BUY: "Compra", KIND_SELL: "Venta", KIND_DIVIDEND: "Dividendo",
               KIND_IGNORE: "Ignorar"}
COL_KIND, COL_QTY = 2, 3


def _item(text: str, right: bool = False, color: Optional[str] = None,
          tooltip: Optional[str] = None, editable: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if color:
        item.setForeground(QColor(color))
    if tooltip:
        item.setToolTip(tooltip)
    flags = item.flags()
    item.setFlags(flags | Qt.ItemFlag.ItemIsEditable if editable
                  else flags & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _table(headers) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                          | QAbstractItemView.EditTrigger.EditKeyPressed)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(36)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    return table


def _parse_quantity(text: str) -> Optional[float]:
    text = (text or "").strip().replace(".", "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


class ImportStatementDialog(QDialog):

    def __init__(self, session: Session, user_id: int, username: str,
                 provider: MarketDataProvider, theme: dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session = session
        self._user_id = user_id
        self._provider = provider
        self._t = theme
        self._runner = BackgroundRunner(self)
        self._prices = BackgroundRunner(self)
        self.statement = None
        self.summary: Optional[ImportSummary] = None
        self._combos: Dict[str, QComboBox] = {}
        self._known_refs: set = set()
        self._filling = False
        self._estimate_timer = QTimer(self)
        self._estimate_timer.setSingleShot(True)
        self._estimate_timer.setInterval(500)
        self._estimate_timer.timeout.connect(self._estimate)

        self.setWindowTitle("Importar extracto del bróker")
        self.setModal(True)
        self.resize(1120, 740)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title = QLabel(f"Importar extracto del bróker · {username}")
        title.setObjectName("dlgTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Admite el «Estado de cuenta» de DEGIRO y los movimientos de cuenta de MyInvestor "
            "(Excel o CSV, desde la apertura de la cuenta). Las operaciones se añaden al usuario con "
            "el que has iniciado sesión; las ya importadas se reconocen y no se duplican. En "
            "MyInvestor revisa el tipo de cada movimiento y las acciones estimadas (doble clic para "
            "corregirlas).")
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
        self._securities = _table(["Clave", "Valor", "Cotización (Yahoo)", "Comprobación"])
        self._securities.verticalHeader().setDefaultSectionSize(40)    # room for the combos
        layout.addWidget(self._securities, 2)

        self._lbl_ops = QLabel("Operaciones")
        self._lbl_ops.setObjectName("sectionTitle")
        layout.addWidget(self._lbl_ops)
        self._ops = _table(["Fecha", "Valor", "Tipo", "Acciones", "Precio", "Importe (EUR)",
                            "Comisiones / retención", "Estado"])
        self._ops.itemChanged.connect(self._on_item_changed)
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
    @property
    def busy(self) -> bool:
        return self._runner.busy or self._prices.busy or self._estimate_timer.isActive()

    def _set_status(self, text: str, error: bool = False) -> None:
        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(
            f"color: {self._t['expense']}; font-weight: 600;" if error else "")

    def _choose_file(self) -> None:
        start = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, "Extracto del bróker", start, "Extracto DEGIRO / MyInvestor (*.xlsx *.csv)")
        if path:
            self.load_file(path)

    def load_file(self, path) -> None:
        path = Path(path)
        try:
            statement = parse_any(path)
        except (StatementError, OSError) as exc:
            self._set_status(str(exc), error=True)
            return
        self.statement = statement
        self._known_refs = already_imported(self._session, self._user_id, statement)
        self._lbl_file.setText(
            f"{path.name} · {statement.broker} · del {fmt_date(statement.first_day)} "
            f"al {fmt_date(statement.last_day)}")
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
        if self.statement.needs_prices:
            self._estimate()
        self._update_ready()

    # -- securities ------------------------------------------------------
    def _fill_securities(self, choices) -> None:
        products = self.statement.products
        self._securities.setRowCount(len(products))
        self._combos = {}
        for row, (key, info) in enumerate(sorted(products.items(), key=lambda kv: kv[1]["name"])):
            choice = choices.get(key)
            label = key[5:] if key.startswith("NAME:") else key
            spellings = " / ".join(info.get("spellings") or [info["name"]])
            self._securities.setItem(row, 0, _item(label if not key.startswith("NAME:") else
                                                   ("Fondo" if info.get("fund") else "Nombre")))
            self._securities.setItem(row, 1, _item(info["name"], tooltip=spellings))
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            detail = "Buscando…"
            if choice is not None:
                for candidate in choice.candidates:
                    combo.addItem(f"{candidate.symbol}", candidate.symbol)
                    combo.setItemData(combo.count() - 1,
                                      f"{candidate.name} · {candidate.exchange} · {candidate.gap_text}",
                                      Qt.ItemDataRole.ToolTipRole)
                if choice.chosen and combo.findData(choice.chosen) < 0:
                    combo.insertItem(0, choice.chosen, choice.chosen)
                combo.setCurrentText(choice.chosen or "")
                best = next((c for c in choice.candidates if c.symbol == choice.chosen), None)
                if choice.from_catalogue:
                    detail = "Ya está en tu catálogo"
                elif best is None:
                    detail = "Sin coincidencia: escribe el símbolo de Yahoo"
                elif best.gap is not None:
                    detail = f"{best.exchange} · precio {best.gap_text}"
                else:
                    detail = f"{best.name} · {best.exchange} · compruébalo"
            combo.currentTextChanged.connect(lambda _t, k=key: self._on_symbol_edited(k))
            self._combos[key] = combo
            self._securities.setCellWidget(row, 2, combo)
            self._securities.setItem(row, 3, _item(detail))
        self._fit(self._securities, stretch=3)
        self._securities.setColumnWidth(2, 220)

    def _row_of(self, key: str) -> int:
        keys = sorted(self.statement.products, key=lambda k: self.statement.products[k]["name"])
        return keys.index(key)

    def _on_symbol_edited(self, key: str) -> None:
        text = self._combos[key].currentText().strip().upper()
        currency = self.statement.products[key]["currency"]
        listing = listing_currency(text) if text else None
        note = ("Cotiza en otra divisa que la compra" if listing and listing != currency
                else "Elegido a mano")
        self._securities.setItem(self._row_of(key), 3, _item(
            note, color=self._t["warning"] if "divisa" in note else None))
        if self.statement.needs_prices:
            self._estimate_timer.start()
        self._update_ready()

    # -- quantity estimation (MyInvestor) --------------------------------
    def _estimate(self) -> None:
        self._estimate_timer.stop()
        statement, symbols, provider = self.statement, self.symbols(), self._provider
        self._set_status("Estimando las acciones con los precios de Yahoo Finance…")
        self._prices.run(lambda: fetch_closes(provider, statement, symbols),
                         lambda closes, error: self._on_closes(symbols, closes, error),
                         latest_only=True)

    def _on_closes(self, symbols, closes, error) -> None:
        if error is not None:
            self._set_status(f"No se pudieron obtener precios para estimar las acciones ({error}). "
                             "Escríbelas a mano con doble clic.", error=True)
            closes = {}
        estimate_quantities(self.statement, symbols, closes)
        self._fill_operations()
        self._update_ready()

    # -- operations ------------------------------------------------------
    def _fill_operations(self) -> None:
        statement, t = self.statement, self._t
        self._filling = True
        rows = sorted(statement.rows, key=lambda r: (r.day, r.ref))
        self._rows = rows
        self._ops.setRowCount(len(rows))
        for index, row in enumerate(rows):
            known = row.ref in self._known_refs
            if known:
                state = _item("Ya importada", color=t["text_secondary"])
            elif row.kind == KIND_IGNORE:
                state = _item("Se ignora", color=t["text_secondary"], tooltip=row.note)
            else:
                state = _item("Nueva", color=t["income"], tooltip=row.note or None)
            trade = row.kind in (KIND_BUY, KIND_SELL)
            if trade and row.quantity:
                qty_text = fmt_quantity(row.quantity)
            elif trade:
                qty_text = "¿?"
            else:
                qty_text = "—"
            qty = _item(qty_text, right=True, editable=trade and not known,
                        color=t["warning"] if trade and (row.quantity_estimated or not row.quantity)
                        else None,
                        tooltip=("Estimada: doble clic para corregirla" if row.quantity_estimated
                                 else None))
            sign = -1 if row.kind == KIND_BUY else 1
            amount = row.amount_eur
            values = [
                _item(fmt_date(row.day)), _item(row.name, tooltip=row.note or None), None, qty,
                _item(fmt_money(row.price, row.currency, 2) if trade and row.price else
                      (row.gross_text or "—"), right=True),
                _item("se estimará" if amount is None else
                      fmt_eur(sign * amount, signed=True) if row.kind != KIND_IGNORE else fmt_eur(amount),
                      right=True, color=(t["text_secondary"] if row.kind == KIND_IGNORE else
                                         t["expense"] if row.kind == KIND_BUY else t["income"])),
                _item(fmt_eur(row.fees_eur) if row.fees_eur else
                      fmt_eur(row.withheld_eur) if row.withheld_eur else "—", right=True),
                state,
            ]
            for col, item in enumerate(values):
                if item is not None:
                    self._ops.setItem(index, col, item)
            if row.kind_editable and not known:
                combo = QComboBox()
                for kind, label in KIND_LABELS.items():
                    combo.addItem(label, kind)
                combo.setCurrentIndex(combo.findData(row.kind))
                combo.currentIndexChanged.connect(
                    lambda _i, r=row, c=combo: self._on_kind_changed(r, c.currentData()))
                self._ops.setCellWidget(index, COL_KIND, combo)
            else:
                self._ops.removeCellWidget(index, COL_KIND)
                self._ops.setItem(index, COL_KIND, _item(
                    KIND_LABELS[row.kind] + (f" ({row.fills} ejec.)" if row.fills > 1 else "")))
        self._filling = False
        self._fit(self._ops, stretch=1)
        self._ops.setColumnWidth(COL_KIND, max(self._ops.columnWidth(COL_KIND), 120))
        new = [r for r in rows if r.ref not in self._known_refs and r.kind != KIND_IGNORE]
        trades = sum(1 for r in new if r.kind in (KIND_BUY, KIND_SELL))
        self._lbl_ops.setText(f"Movimientos · {trades} compras/ventas y {len(new) - trades} "
                              f"dividendos nuevos · {len(rows) - len(new)} ya importados o ignorados")
        self._new = len(new)

    def _on_kind_changed(self, row, kind) -> None:
        row.kind = kind
        if kind in (KIND_BUY, KIND_SELL) and not row.quantity:
            row.quantity_estimated = True
            self._estimate_timer.start()
        if kind not in (KIND_BUY, KIND_SELL):
            row.quantity = row.price = None
        QTimer.singleShot(0, self._refresh_after_edit)

    def _refresh_after_edit(self) -> None:
        self._fill_operations()
        self._update_ready()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.column() != COL_QTY:
            return
        row = self._rows[item.row()]
        quantity = _parse_quantity(item.text())
        row.quantity = quantity
        row.quantity_estimated = False           # typed by the user: keep it
        row.price = round(row.amount_eur / quantity, 4) if quantity and row.amount_eur else None
        QTimer.singleShot(0, self._refresh_after_edit)

    @staticmethod
    def _fit(table: QTableWidget, stretch: int) -> None:
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)

    def symbols(self) -> Dict[str, str]:
        return {key: combo.currentText().strip().upper() for key, combo in self._combos.items()}

    def _update_ready(self) -> None:
        if self.statement is None:
            return
        used = {r.key for r in self.statement.rows if r.kind != KIND_IGNORE
                and r.ref not in self._known_refs}
        missing = [k for k, s in self.symbols().items() if not s and k in used]
        no_qty = [r for r in self.statement.rows if r.kind in (KIND_BUY, KIND_SELL)
                  and not r.quantity and r.ref not in self._known_refs]
        if missing:
            text = f"Falta el símbolo de {len(missing)} valor(es)."
        elif no_qty:
            text = (f"Faltan las acciones de {len(no_qty)} movimiento(s): escríbelas con doble clic "
                    "en la columna «Acciones».")
        elif not self._new:
            text = "Todas las operaciones de este extracto ya estaban importadas."
        else:
            text = f"Listo para importar {self._new} movimientos nuevos."
        if self.statement.ignored:
            text += " Se ignoran: " + ", ".join(
                f"{k} ({v})" for k, v in self.statement.ignored.items()) + "."
        if self.statement.warnings:
            text += " Avisos: " + " ".join(self.statement.warnings)
        self._set_status(text)
        self._btn_import.setEnabled(bool(self._new) and not missing and not no_qty
                                    and not self.busy)

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
        self._estimate_timer.stop()
        self._runner.shutdown()
        self._prices.shutdown()
        super().done(result)
