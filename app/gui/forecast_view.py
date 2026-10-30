"""
app/gui/forecast_view.py
========================
FAMILYFIN — Previsiones (desktop): fixed-spending baseline, this month's
bills, next month's forecast and the aportes between members. The numbers
come from app.analytics.forecast, the same service as the web /prevision.

Mío = the signed-in member; Familia = every member except the demo account,
aggregates only (never another member's movements).
"""
from __future__ import annotations

import logging
from typing import List, Optional

import qtawesome as qta
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSizePolicy, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from sqlalchemy.orm import Session

from app.analytics.forecast import STATUS_CHARGED, Forecast, family_forecast, user_forecast
from app.categorization.admin_service import NATURE_LABELS, set_natures, suggested_natures
from app.categorization.rules_engine import shares_global_rules
from app.config import THEMES
from app.db.models import Category, User
from app.gui._style_utils import apply_card_shadow
from app.gui.dashboard import _KPICard, _PaintCanvas, _fmt_currency_short

logger = logging.getLogger(__name__)


def _eur(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


class _SplitChart(QFrame):
    """Fixed vs variable spending per month — stacked bars (QPainter)."""

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self._t = theme
        self._data: List[tuple] = []          # (label, fixed, variable)
        self.setObjectName("chartCard")
        self.setMinimumHeight(250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        hdr = QHBoxLayout()
        title = QLabel("Fijos y variables por mes")
        title.setObjectName("chartTitle")
        hdr.addWidget(title)
        hdr.addStretch()
        self._legend: List[QLabel] = []
        for key, text in (("accent", "Fijos"), ("expense", "Variables")):
            dot = QLabel("●")
            dot.setProperty("colorKey", key)
            self._legend.append(dot)
            hdr.addWidget(dot)
            lbl = QLabel(text)
            lbl.setObjectName("chartLegendLabel")
            hdr.addWidget(lbl)
            hdr.addSpacing(8)
        root.addLayout(hdr)
        self._canvas = _PaintCanvas(self._paint)
        self._canvas.setMinimumHeight(190)
        root.addWidget(self._canvas)
        self.apply_theme(theme)

    def set_data(self, data: List[tuple]) -> None:
        self._data = data
        self._canvas.update()

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        for dot in self._legend:
            dot.setStyleSheet(f"color: {theme[dot.property('colorKey')]}; font-size: 11px;")
        apply_card_shadow(self, theme)
        self._canvas.update()

    def _paint(self, painter: QPainter, rect: QRectF) -> None:
        t = self._t
        if not self._data:
            painter.setPen(QColor(t["text_secondary"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Sin datos")
            return
        pad_l, pad_b, pad_t = 44, 22, 6
        w, h = rect.width() - pad_l - 8, rect.height() - pad_b - pad_t
        top = max((f + v for _, f, v in self._data), default=1.0) or 1.0
        painter.setFont(QFont("Segoe UI", 7))
        for step in (0.25, 0.5, 0.75, 1.0):
            gy = rect.y() + pad_t + h * (1 - step)
            painter.setPen(QColor(t["text_secondary"]))
            painter.drawText(QRectF(rect.x(), gy - 8, pad_l - 8, 16),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             _fmt_currency_short(top * step))
            painter.setPen(QPen(QColor(t["border"]), 0.5, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(rect.x() + pad_l, gy), QPointF(rect.right() - 8, gy))
        slot = w / len(self._data)
        bar = max(6.0, slot * 0.5)
        for i, (label, fixed, variable) in enumerate(self._data):
            x = rect.x() + pad_l + i * slot + (slot - bar) / 2
            base = rect.y() + pad_t + h
            painter.setPen(Qt.PenStyle.NoPen)
            for value, key in ((fixed, "accent"), (variable, "expense")):
                bh = h * max(0.0, value) / top
                painter.setBrush(QBrush(QColor(t[key])))
                painter.drawRoundedRect(QRectF(x, base - bh, bar, bh), 2, 2)
                base -= bh
            painter.setPen(QColor(t["text_secondary"]))
            painter.drawText(QRectF(x - slot / 2, rect.y() + pad_t + h + 4, bar + slot, 16),
                             Qt.AlignmentFlag.AlignHCenter, label)


class ForecastView(QWidget):

    def __init__(self, user: User, session: Session, theme: str = "light") -> None:
        super().__init__()
        self._user = user
        self._session = session
        self._theme = theme
        self._t = THEMES[theme]
        self._family_allowed = shares_global_rules(session, user.id)
        self._build_ui()
        self._apply_theme()

    # ------------------------------------------------------------------
    # Qt lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_data()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QFrame()
        top.setObjectName("dashTopBar")
        top.setFixedHeight(52)
        bar = QHBoxLayout(top)
        bar.setContentsMargins(24, 0, 24, 0)
        title = QLabel("Previsiones")
        title.setObjectName("dashTitle")
        bar.addWidget(title)
        bar.addSpacing(16)
        self._lbl_window = QLabel("")
        self._lbl_window.setObjectName("dashRangeLabel")
        bar.addWidget(self._lbl_window)
        bar.addStretch()
        self._cmb_view = QComboBox()
        self._cmb_view.setObjectName("dashCombo")
        self._cmb_view.addItem("Mío", "mio")
        self._cmb_view.addItem("Familia", "familia")
        self._cmb_view.setToolTip("Familia: todos los miembros sumados; solo totales, nunca sus movimientos.")
        self._cmb_view.setVisible(self._family_allowed)
        self._cmb_view.currentIndexChanged.connect(lambda _i: self.refresh_data())
        bar.addWidget(self._cmb_view)
        self._btn_refresh = QPushButton()
        self._btn_refresh.setFixedSize(32, 32)
        self._btn_refresh.setObjectName("btnSecondary")
        self._btn_refresh.setToolTip("Actualizar")
        self._btn_refresh.clicked.connect(self.refresh_data)
        bar.addWidget(self._btn_refresh)
        root.addWidget(top)

        scroll = QScrollArea()
        scroll.setObjectName("dashScroll")
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("dashContent")
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(24, 16, 24, 24)
        self._body.setSpacing(14)
        scroll.setWidget(content)
        root.addWidget(scroll)

        # Suggestions (no category marked yet)
        self._suggest_box = QFrame()
        self._suggest_box.setObjectName("chartCard")
        sb = QVBoxLayout(self._suggest_box)
        sb.setContentsMargins(20, 14, 20, 14)
        lbl = QLabel("Marca tus gastos fijos")
        lbl.setObjectName("chartTitle")
        sb.addWidget(lbl)
        note = QLabel("Estas categorías parecen recibos que se repiten o traspasos entre vosotros. "
                      "Desmarca las que no lo sean y confirma; luego puedes cambiarlas en Categorías.")
        note.setWordWrap(True)
        note.setObjectName("noteLabel")
        sb.addWidget(note)
        self._suggest_list = QListWidget()
        self._suggest_list.setMinimumHeight(160)
        sb.addWidget(self._suggest_list)
        self._btn_mark = QPushButton("  Marcar las seleccionadas")
        self._btn_mark.setObjectName("btnPrimary")
        self._btn_mark.setFixedHeight(34)
        self._btn_mark.clicked.connect(self._on_mark)
        sb.addWidget(self._btn_mark, alignment=Qt.AlignmentFlag.AlignLeft)
        self._body.addWidget(self._suggest_box)

        self._lbl_empty = QLabel("Aún no hay datos suficientes: hacen falta al menos 3 meses completos "
                                 "de movimientos para calcular una media fiable.")
        self._lbl_empty.setObjectName("noteLabel")
        self._lbl_empty.setWordWrap(True)
        self._body.addWidget(self._lbl_empty)

        # KPI cards
        self._cards_box = QWidget()
        grid = QGridLayout(self._cards_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        t = self._t
        specs = [
            ("next", "Gastos previstos", "mdi.calendar-arrow-right", t["accent"]),
            ("fixed", "Gasto fijo medio", "mdi.pin", t["accent"]),
            ("variable", "Gasto variable", "mdi.cart-outline", t["expense"]),
            ("income", "Ingresos previstos", "mdi.trending-up", t["income"]),
            ("saving", "Ahorro previsto", "mdi.piggy-bank-outline", t["income"]),
            ("needed", "Aportación necesaria", "mdi.swap-horizontal", t["investment"]),
            ("received", "Aportaciones recibidas", "mdi.account-cash-outline", t["investment"]),
        ]
        self._cards = {}
        for i, (key, label, icon, color) in enumerate(specs):
            card = _KPICard(label, icon, color, t)
            self._cards[key] = card
            grid.addWidget(card, i // 4, i % 4)
        self._body.addWidget(self._cards_box)

        self._lbl_warnings = QLabel("")
        self._lbl_warnings.setObjectName("warnLabel")
        self._lbl_warnings.setWordWrap(True)
        self._body.addWidget(self._lbl_warnings)

        # This month / next month
        months = QHBoxLayout()
        months.setSpacing(14)
        self._lbl_this, self._tbl_this = self._table_box(months, ["Recibo", "Categoría", "Estado", "Importe"])
        self._lbl_next, self._tbl_next = self._table_box(months, ["Día", "Recibo", "Categoría", "Importe"],
                                                         wide_col=1)
        self._body.addLayout(months)

        self._chart = _SplitChart(self._t)
        self._body.addWidget(self._chart)

        series_row = QHBoxLayout()
        series_row.setSpacing(14)
        self._lbl_series, self._tbl_series = self._table_box(
            series_row, ["Recibo", "Categoría", "Frecuencia", "Por cargo", "Media / mes", "Próximo"], stretch=3)
        self._lbl_cats, self._tbl_cats = self._table_box(series_row, ["Categoría", "Media / mes"], stretch=1)
        self._body.addLayout(series_row)

        aport = QHBoxLayout()
        self._lbl_aport, self._tbl_aport = self._table_box(
            aport, ["De → a", "Este mes", "Mes pasado", "Media / mes", "Total", "Parte"])
        self._aport_row = aport
        self._body.addLayout(aport)
        self._lbl_mismatch = QLabel("")
        self._lbl_mismatch.setObjectName("warnLabel")
        self._lbl_mismatch.setWordWrap(True)
        self._body.addWidget(self._lbl_mismatch)

        self._lbl_foot = QLabel("")
        self._lbl_foot.setObjectName("noteLabel")
        self._lbl_foot.setWordWrap(True)
        self._body.addWidget(self._lbl_foot)
        self._body.addStretch()

    def _table_box(self, row: QHBoxLayout, headers: List[str], stretch: int = 1, wide_col: int = 0):
        box = QFrame()
        box.setObjectName("chartCard")
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("")
        title.setObjectName("chartTitle")
        title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(title)
        table = QTableWidget(0, len(headers))
        table.setObjectName("recentTable")
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(wide_col, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)
        layout.addStretch(1)                    # title and table stay at the top of the card
        row.addWidget(box, stretch)
        apply_card_shadow(box, self._t)
        return title, table

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh_data(self) -> None:
        familia = self._cmb_view.currentData() == "familia" and self._family_allowed
        try:
            fc = family_forecast(self._session) if familia else user_forecast(self._session, self._user.id)
        except Exception:
            self._session.rollback()
            logger.exception("Previsiones: no se pudo calcular")
            return
        self._load_suggestions(familia)
        self._show(fc, familia)

    def _load_suggestions(self, familia: bool) -> None:
        self._suggest_list.clear()
        marked = self._session.query(Category.id).filter(
            Category.user_id == self._user.id, Category.nature.isnot(None)).first() is not None
        found = {} if familia or marked else suggested_natures(self._session, self._user.id)
        names = dict(self._session.query(Category.id, Category.name).filter(Category.id.in_(found)))
        for cid, nature in sorted(found.items(), key=lambda kv: (kv[1], names[kv[0]].lower())):
            item = QListWidgetItem(f"{names[cid]}  ·  {NATURE_LABELS[nature]}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, (cid, nature))
            self._suggest_list.addItem(item)
        self._suggest_box.setVisible(bool(found))

    def _on_mark(self) -> None:
        natures = {}
        for i in range(self._suggest_list.count()):
            item = self._suggest_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                cid, nature = item.data(Qt.ItemDataRole.UserRole)
                natures[cid] = nature
        if not natures:
            return
        try:
            set_natures(self._session, self._user.id, natures)
        except Exception:
            self._session.rollback()
            logger.exception("Previsiones: no se pudieron marcar las categorías")
            return
        self.refresh_data()

    def _show(self, fc: Forecast, familia: bool) -> None:
        enough = fc.enough_data
        self._lbl_empty.setVisible(not enough)
        for widget in (self._cards_box, self._tbl_this.parentWidget(), self._tbl_next.parentWidget(),
                       self._chart, self._tbl_series.parentWidget(), self._tbl_cats.parentWidget(),
                       self._lbl_foot):
            widget.setVisible(enough)
        self._lbl_window.setText(
            (f"{fc.months} meses completos" + (" · provisional" if fc.provisional else "")
             + (f" · {', '.join(fc.members)}" if familia else "")) if enough else "")
        if not enough:
            self._tbl_aport.parentWidget().setVisible(False)
            self._lbl_warnings.setVisible(False)
            self._lbl_mismatch.setVisible(False)
            return

        c = self._cards
        c["next"].set_value(_eur(fc.next_expenses), fc.next_month_label)
        c["fixed"].set_value(_eur(fc.fixed_baseline), "por mes, no mensuales repartidos")
        c["variable"].set_value(_eur(fc.variable_median), f"mediana · media {_eur(fc.variable_mean)}")
        c["income"].set_value(_eur(fc.next_income), fc.next_month_label)
        c["saving"].set_value(_eur(fc.next_saving), "ingresos − gastos previstos")
        receives = fc.receives_transfers and not familia
        c["needed"].setVisible(receives)
        c["received"].setVisible(receives)
        if receives:
            c["needed"].set_value(_eur(fc.required_contribution), "para cubrir los gastos")
            gap = fc.contribution_gap
            c["received"].set_value(_eur(fc.transfers_in_avg),
                                    ("sobran " if gap >= 0 else "faltan ") + _eur(abs(gap)) + " al mes",
                                    badge="OK" if gap >= 0 else "Falta", badge_positive=gap >= 0)

        warnings = list(fc.warnings)
        if fc.uncategorized:
            warnings.append(f"{fc.uncategorized} movimientos sin categoría cuentan como variables.")
        if not familia and fc.transfers_out_avg:
            warnings.append(f"Tus aportaciones (traspasos): {_eur(fc.transfers_out_avg)} al mes de media, "
                            "fuera de tus gastos.")
        self._lbl_warnings.setText("\n".join(f"⚠ {w}" for w in warnings))
        self._lbl_warnings.setVisible(bool(warnings))

        pending = f" · pendiente {_eur(fc.pending_this_month)}" if fc.pending_this_month else ""
        self._lbl_this.setText(f"Este mes · {fc.this_month_label}{pending}")
        rows = []
        for i in fc.this_month:
            status = ("Cargado" if i.status == STATUS_CHARGED else f"Pendiente · hacia el {i.day}")
            if i.above_pct:
                status += f" · +{i.above_pct:.0f} % sobre lo habitual"
            rows.append([i.label, i.category, status, _eur(i.actual if i.actual is not None else i.expected)])
        self._fill(self._tbl_this, rows or [["Nada previsto este mes", "", "", ""]])

        self._lbl_next.setText(f"Mes próximo · {fc.next_month_label}")
        rows = [[str(i.day or ""), i.label, i.category, _eur(i.expected)] for i in fc.next_month]
        if fc.next_other_fixed:
            rows.append(["≈", "Otros fijos sin fecha fija", "media mensual", _eur(fc.next_other_fixed)])
        rows.append(["≈", "Gasto variable estimado", "mediana 6 meses", _eur(fc.variable_median)])
        rows.append(["", "Total gastos previstos", "", _eur(fc.next_expenses)])
        self._fill(self._tbl_next, rows, bold_last=True)

        self._chart.set_data([(s.label, s.fixed, s.variable) for s in fc.monthly_split])

        self._lbl_series.setText(f"Recibos fijos · {len(fc.series)}")
        self._fill(self._tbl_series, [
            [s.label, s.category, s.period_label, _eur(s.usual), _eur(s.monthly),
             (s.next_due.strftime("%d/%m/%Y") if s.active and s.next_due
              else ("sin cargos desde " + s.last_date.strftime("%d/%m/%Y") if not s.active else "—"))]
            for s in fc.series])
        self._lbl_cats.setText("Gasto fijo por categoría")
        self._fill(self._tbl_cats, [[name, _eur(value)] for name, value in fc.fixed_by_category])

        has_aport = bool(fc.contributions)
        self._tbl_aport.parentWidget().setVisible(has_aport)
        self._lbl_aport.setText("Aportaciones · traspasos entre miembros (no cuentan como gasto ni ingreso)")
        self._fill(self._tbl_aport, [
            [f"{a.sender} → {a.receiver}", _eur(a.this_month), _eur(a.last_month), _eur(a.average),
             _eur(a.total), f"{a.share_pct:.1f} %".replace(".", ",") if a.share_pct is not None else "—"]
            for a in fc.contributions])
        mismatches = [f"⚠ {a.sender} → {a.receiver}: no cuadra en {'; '.join(a.mismatches)}"
                      for a in fc.contributions if a.mismatches]
        self._lbl_mismatch.setText("\n".join(mismatches))
        self._lbl_mismatch.setVisible(bool(mismatches))

        self._lbl_foot.setText(
            f"Los fijos son los movimientos de categorías marcadas como Fijo (o marcados a mano). La media "
            f"usa los últimos {fc.months} meses completos: el mes en curso no cuenta. Los recibos que no "
            "llegan cada mes se reparten en la media y en la previsión solo aparecen el mes que tocan."
            + (" En Familia solo se ven totales." if familia else ""))

    def _fill(self, table: QTableWidget, rows: List[list], bold_last: bool = False) -> None:
        table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if value.endswith("€") or value.endswith("%"):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if bold_last and r == len(rows) - 1:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                table.setItem(r, col, item)
        table.resizeRowsToContents()
        # Exactly its rows (no inner scrollbar, no empty band); long lists scroll.
        height = (table.horizontalHeader().sizeHint().height() + table.verticalHeader().length()
                  + 2 * table.frameWidth() + 2)
        table.setFixedHeight(min(460, height))

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def apply_theme(self, theme_key: str) -> None:
        self._theme = theme_key
        self._t = THEMES[theme_key]
        self._apply_theme()

    def _apply_theme(self) -> None:
        t = self._t
        self._btn_refresh.setIcon(qta.icon("mdi.refresh", color=t["text_secondary"]))
        for card in self._cards.values():
            card.apply_style(t)
        self._chart.apply_theme(t)
        self.setStyleSheet(f"""
            QWidget#dashContent, QScrollArea#dashScroll {{ background-color: {t["bg_primary"]}; border: none; }}
            QFrame#dashTopBar {{ background-color: {t["bg_secondary"]}; border-bottom: 1px solid {t["border"]}; }}
            QLabel#dashTitle {{ color: {t["text_primary"]}; font-size: 16px; font-weight: 700; }}
            QLabel#dashRangeLabel, QLabel#noteLabel {{ color: {t["text_secondary"]}; font-size: 12px; }}
            QLabel#warnLabel {{ color: {t["warning"]}; font-size: 12px; }}
            QLabel {{ background: transparent; }}
            QComboBox#dashCombo {{ background-color: {t["bg_secondary"]}; border: 1px solid {t["border"]};
                border-radius: 6px; padding: 4px 10px; color: {t["text_primary"]}; font-size: 12px;
                min-width: 110px; }}
            QFrame#chartCard {{ background-color: {t["bg_secondary"]}; border-radius: 12px;
                border: 1px solid {t["border"]}; }}
            QLabel#chartTitle {{ color: {t["text_primary"]}; font-size: 13px; font-weight: 600; }}
            QLabel#chartLegendLabel {{ color: {t["text_secondary"]}; font-size: 11px; }}
            QListWidget {{ background-color: {t["bg_primary"]}; color: {t["text_primary"]};
                border: 1px solid {t["border"]}; border-radius: 8px; font-size: 12px; }}
            QPushButton#btnPrimary {{ background-color: {t["accent"]}; color: #FFFFFF; border: none;
                border-radius: 6px; font-size: 13px; font-weight: 600; padding: 6px 16px; }}
            QPushButton#btnSecondary {{ background-color: {t["bg_secondary"]}; border: 1px solid {t["border"]};
                border-radius: 6px; }}
            QTableWidget#recentTable {{ background-color: {t["bg_primary"]};
                alternate-background-color: {t["bg_secondary"]}; border: 1px solid {t["border"]};
                border-radius: 8px; gridline-color: {t["border"]}; font-size: 12px; color: {t["text_primary"]}; }}
            QHeaderView::section {{ background-color: {t["bg_hover"]}; color: {t["text_secondary"]};
                font-size: 11px; font-weight: 600; border: none; padding: 5px 8px; }}
        """)
