"""
app/gui/dashboard.py
=====================
FAMILYFIN — Dashboard View (Posición Global)

KPI cards:
  - Saldo actual / Ingresos / Gastos / Inversiones / Tasa ahorro / Sin categorizar

Charts (Phase 5 — implemented):
  - Gastos por categoría: Donut (QPainter) + Barras horizontales, toggle button
  - Evolución mensual ingresos vs gastos: Barras agrupadas 12 meses (QPainter)
  - Últimas 5 transacciones: mini-tabla embebida

Range selector: Mes actual | Año actual | Todo
"""
from __future__ import annotations

import logging
import math
from datetime    import date, datetime
from typing      import List, Optional, Tuple

from PySide6.QtCore    import Qt, QRectF, QPointF
from PySide6.QtGui     import (
    QFont, QColor, QPainter, QPen, QBrush,
    QPainterPath, QFontMetrics,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QScrollArea, QComboBox, QPushButton,
    QSizePolicy, QGridLayout, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
)
import qtawesome as qta

from sqlalchemy.orm import Session

from app.analytics.dashboard_summary import dashboard_summary, range_bounds
from app.config    import THEMES
from app.db.models import User
from app.gui._style_utils import apply_card_shadow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Palette helpers
# ---------------------------------------------------------------------------

_CHART_PALETTE = [
    "#2563EB", "#16A34A", "#DC2626", "#D97706",
    "#7C3AED", "#0891B2", "#DB2777", "#65A30D",
    "#EA580C", "#6366F1",
]

# Colour override for special labels (prefix-matched)
_CHART_COLOR_OVERRIDES: dict[str, str] = {
    "Sin categorizar": "#94A3B8",   # slate-400 — neutral grey
}


def _chart_color(index: int, label: str) -> QColor:
    """Return palette colour, with overrides for special labels."""
    for prefix, hex_color in _CHART_COLOR_OVERRIDES.items():
        if label.startswith(prefix):
            return QColor(hex_color)
    return QColor(_CHART_PALETTE[index % len(_CHART_PALETTE)])


# ---------------------------------------------------------------------------
# KPI Card widget
# ---------------------------------------------------------------------------

class _KPICard(QFrame):
    """
    A single KPI metric card.

    ┌────────────────────────────┐
    │  🔵  Label          ↑+5%  │
    │      €1.234,56             │
    │      sub-label             │
    └────────────────────────────┘
    """

    def __init__(self, label: str, icon_name: str,
                 accent_color: str, theme: dict) -> None:
        super().__init__()
        self._theme   = theme
        self._accent  = accent_color
        self.setObjectName("kpiCard")
        self.setMinimumWidth(200)
        self.setMinimumHeight(110)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(6)

        # Header row: icon + label + badge
        header = QHBoxLayout()
        header.setSpacing(8)

        lbl_icon = QLabel()
        lbl_icon.setPixmap(
            qta.icon(icon_name, color=accent_color).pixmap(20, 20)
        )
        header.addWidget(lbl_icon)

        self._lbl_label = QLabel(label)
        self._lbl_label.setObjectName("kpiLabel")
        header.addWidget(self._lbl_label)

        header.addStretch()

        self._lbl_badge = QLabel("")
        self._lbl_badge.setObjectName("kpiBadge")
        self._lbl_badge.setVisible(False)
        header.addWidget(self._lbl_badge)

        layout.addLayout(header)

        # Value
        self._lbl_value = QLabel("—")
        self._lbl_value.setObjectName("kpiValue")
        layout.addWidget(self._lbl_value)

        # Sub-label
        self._lbl_sub = QLabel("")
        self._lbl_sub.setObjectName("kpiSub")
        layout.addWidget(self._lbl_sub)

    def set_value(self, value: str, sub: str = "",
                  badge: str = "", badge_positive: bool = True) -> None:
        self._lbl_value.setText(value)
        self._lbl_sub.setText(sub)
        if badge:
            self._lbl_badge.setText(badge)
            color = (self._theme["income"]
                     if badge_positive else self._theme["expense"])
            self._lbl_badge.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: 600;"
            )
            self._lbl_badge.setVisible(True)
        else:
            self._lbl_badge.setVisible(False)

    def apply_style(self, theme: dict) -> None:
        self._theme = theme
        t = theme
        self.setStyleSheet(f"""
            QFrame#kpiCard {{
                background-color: {t["bg_secondary"]};
                border-radius: 12px;
                border: 1px solid {t["border"]};
            }}
            QLabel#kpiLabel {{
                color: {t["text_secondary"]};
                font-size: 12px;
                font-weight: 500;
            }}
            QLabel#kpiValue {{
                color: {t["text_primary"]};
                font-size: 22px;
                font-weight: 700;
            }}
            QLabel#kpiSub {{
                color: {t["text_secondary"]};
                font-size: 11px;
            }}
        """)
        apply_card_shadow(self, theme)


# ---------------------------------------------------------------------------
# Section header
# ---------------------------------------------------------------------------
# Chart widget — Donut / Barras horizontales con toggle
# ---------------------------------------------------------------------------

class _CategoryChartWidget(QFrame):
    """
    Gastos por categoría con toggle Donut ↔ Barras horizontales.
    Recibe datos via set_data(). Se repinta al cambiar de modo.
    """

    MODE_DONUT = "donut"
    MODE_HBAR  = "hbar"

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self._t      = theme
        self._mode   = self.MODE_DONUT
        self._data:  List[Tuple[str, float]] = []   # (label, value)
        self._total: float = 0.0

        self.setObjectName("chartCard")
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        # ── Header row ────────────────────────────────────────────────
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(8)

        hdr = QHBoxLayout()
        self._lbl_title = QLabel("Gastos por categoría")
        self._lbl_title.setObjectName("chartTitle")
        hdr.addWidget(self._lbl_title)
        hdr.addStretch()

        self._btn_toggle = QPushButton()
        self._btn_toggle.setFixedSize(28, 28)
        self._btn_toggle.setFlat(True)
        self._btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_toggle.clicked.connect(self._toggle_mode)
        hdr.addWidget(self._btn_toggle)
        root.addLayout(hdr)

        # Keep the card size unchanged while allowing the bars to grow
        # inside a vertical scroll area when there are more categories.
        self._canvas = _PaintCanvas(self._paint_chart)
        self._canvas.setMinimumHeight(200)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll.setWidget(self._canvas)
        root.addWidget(self._scroll)

        self._update_toggle_icon()
        apply_card_shadow(self, self._t)

    def _toggle_mode(self) -> None:
        self._mode = (self.MODE_HBAR
                      if self._mode == self.MODE_DONUT
                      else self.MODE_DONUT)
        self._update_canvas_height()
        self._update_toggle_icon()
        self._canvas.update()

    def _update_toggle_icon(self) -> None:
        icon_name = ("mdi.chart-bar" if self._mode == self.MODE_DONUT
                     else "mdi.chart-donut")
        self._btn_toggle.setIcon(
            qta.icon(icon_name, color=self._t.get("text_secondary", "#888"))
        )
        tip = ("Ver como barras" if self._mode == self.MODE_DONUT
               else "Ver como donut")
        self._btn_toggle.setToolTip(tip)

    def set_data(self, data: List[Tuple[str, float]]) -> None:
        """data = [(category_name, abs_amount), ...] sorted desc."""
        self._data  = data
        self._total = sum(v for _, v in self._data)
        self._update_canvas_height()
        self._canvas.update()

    def _update_canvas_height(self) -> None:
        if self._mode == self.MODE_HBAR:
            # Keep one readable row per category; the card size remains
            # unchanged and the scroll area exposes the remaining rows.
            height = max(200, len(self._data) * 28 + 20)
        else:
            height = 200
        self._canvas.setMinimumHeight(height)

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        self._update_toggle_icon()
        self._canvas.update()
        apply_card_shadow(self, theme)

    def _paint_chart(self, painter: QPainter,
                     rect: QRectF) -> None:
        if not self._data or self._total == 0:
            self._paint_empty(painter, rect)
            return
        if self._mode == self.MODE_DONUT:
            self._paint_donut(painter, rect)
        else:
            self._paint_hbar(painter, rect)

    def _paint_empty(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QColor(self._t.get("text_secondary", "#888")))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         "Sin datos para el período")

    def _paint_donut(self, painter: QPainter, rect: QRectF) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ── Geometry ─────────────────────────────────────────────────
        legend_w   = max(160, int(rect.width() * 0.38))
        donut_rect = QRectF(rect.x(), rect.y(),
                            rect.width() - legend_w - 8, rect.height())
        cx = donut_rect.center().x()
        cy = donut_rect.center().y()
        r  = min(donut_rect.width(), donut_rect.height()) / 2 - 6
        r  = max(r, 20)
        hole = r * 0.55

        arc_rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        # ── Sectors ──────────────────────────────────────────────────
        start_angle = 90 * 16   # QPainter angles are in 1/16 degree
        for i, (label, value) in enumerate(self._data):
            color  = _chart_color(i, label)
            span   = int(round(value / self._total * 360 * 16))

            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            # Negative span = clockwise, the same direction start_angle moves;
            # a positive span drew each slice back over the previous one.
            painter.drawPie(arc_rect, start_angle, -span)
            start_angle -= span   # clockwise

        # ── Donut hole ───────────────────────────────────────────────
        bg = QColor(self._t.get("bg_secondary", "#FFFFFF"))
        painter.setBrush(QBrush(bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), hole, hole)

        # ── Centre label ─────────────────────────────────────────────
        painter.setPen(QColor(self._t.get("text_primary", "#000")))
        f = QFont("Segoe UI", 9, QFont.Weight.Bold)
        painter.setFont(f)
        painter.drawText(
            QRectF(cx - hole, cy - hole, hole * 2, hole * 2),
            Qt.AlignmentFlag.AlignCenter,
            _fmt_currency_short(self._total),
        )

        # ── Legend ───────────────────────────────────────────────────
        lx = donut_rect.right() + 12
        ly = rect.y() + 8
        line_h = 18
        painter.setFont(QFont("Segoe UI", 8))
        for i, (label, value) in enumerate(self._data):
            color = _chart_color(i, label)
            pct   = value / self._total * 100
            # Colour dot
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(
                QRectF(lx, ly + i * line_h + 4, 8, 8)
            )
            # Text — elide dynamically to the space actually available
            painter.setPen(QColor(self._t.get("text_secondary", "#666")))
            fm = painter.fontMetrics()
            pct_str  = f"  {pct:.1f}%"
            avail_w  = max(0.0, legend_w - 14 - fm.horizontalAdvance(pct_str))
            elided   = fm.elidedText(label, Qt.TextElideMode.ElideRight,
                                     int(avail_w))
            painter.drawText(
                QRectF(lx + 13, ly + i * line_h, legend_w - 14, line_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"{elided}{pct_str}",
            )

    def _paint_hbar(self, painter: QPainter, rect: QRectF) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        n        = len(self._data)
        bar_h    = min(22, max(12, int((rect.height() - 20) / n) - 4))
        label_w  = 130
        val_w    = 70
        bar_area = rect.width() - label_w - val_w - 8
        max_val  = self._data[0][1] if self._data else 1

        painter.setFont(QFont("Segoe UI", 8))

        for i, (label, value) in enumerate(self._data):
            y      = rect.y() + i * (bar_h + 6)
            color  = _chart_color(i, label)

            # Label — elide dynamically based on available column width
            painter.setPen(QColor(self._t.get("text_secondary", "#666")))
            fm = painter.fontMetrics()
            elided = fm.elidedText(label, Qt.TextElideMode.ElideRight,
                                   int(label_w))
            painter.drawText(
                QRectF(rect.x(), y, label_w, bar_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elided,
            )

            # Bar background
            bx = rect.x() + label_w + 4
            bg_color = QColor(self._t.get("border", "#ddd"))
            painter.setBrush(QBrush(bg_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(
                QRectF(bx, y + 4, bar_area, bar_h - 8), 3, 3
            )

            # Bar fill
            fill_w = max(4.0, bar_area * value / max_val)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(
                QRectF(bx, y + 4, fill_w, bar_h - 8), 3, 3
            )

            # Value
            painter.setPen(QColor(self._t.get("text_primary", "#000")))
            painter.drawText(
                QRectF(bx + bar_area + 6, y, val_w, bar_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                _fmt_currency_short(value),
            )


# ---------------------------------------------------------------------------
# Chart widget — Evolución mensual (barras agrupadas)
# ---------------------------------------------------------------------------

class _MonthlyChartWidget(QFrame):
    """
    Ingresos vs Gastos por mes — barras agrupadas, últimos 12 meses.
    """

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self._t = theme
        # List of (month_label, income, expense)
        self._data: List[Tuple[str, float, float]] = []

        self.setObjectName("chartCard")
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(8)

        hdr = QHBoxLayout()
        lbl = QLabel("Ingresos vs Gastos — últimos 12 meses")
        lbl.setObjectName("chartTitle")
        hdr.addWidget(lbl)
        hdr.addStretch()

        # Legend
        for color, text in [
            ("#16A34A", "Ingresos"),
            ("#DC2626", "Gastos"),
        ]:
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color}; font-size: 11px;")
            hdr.addWidget(dot)
            lbl2 = QLabel(text)
            lbl2.setObjectName("chartLegendLabel")
            hdr.addWidget(lbl2)
            hdr.addSpacing(8)

        root.addLayout(hdr)

        self._canvas = _PaintCanvas(self._paint_chart)
        self._canvas.setMinimumHeight(200)
        root.addWidget(self._canvas)
        apply_card_shadow(self, self._t)

    def set_data(self,
                 data: List[Tuple[str, float, float]]) -> None:
        """data = [(month_label, income, expense), ...] oldest→newest."""
        self._data = data[-12:]
        self._canvas.update()

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        self._canvas.update()
        apply_card_shadow(self, theme)

    def _paint_chart(self, painter: QPainter,
                     rect: QRectF) -> None:
        if not self._data:
            painter.setPen(QColor(self._t.get("text_secondary", "#888")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Sin datos para el período")
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        n        = len(self._data)
        pad_l    = 44       # space for Y-axis value labels
        pad_r    = 8
        pad_top  = 8
        pad_bot  = 24       # space for month labels
        usable_w = rect.width()  - pad_l - pad_r
        usable_h = rect.height() - pad_top - pad_bot

        group_w  = usable_w / n
        bar_w    = max(4.0, group_w * 0.32)
        gap      = max(2.0, group_w * 0.06)

        max_val  = max(
            (max(inc, exp) for _, inc, exp in self._data),
            default=1.0
        ) or 1.0

        color_inc = QColor("#16A34A")
        color_exp = QColor("#DC2626")
        color_lbl = QColor(self._t.get("text_secondary", "#888"))
        color_grid = QColor(self._t.get("border", "#ddd"))
        color_axis = QColor(self._t.get("border_strong", "#9098A3"))

        # ── Gridlines + Y-axis value labels ────────────────────────────
        painter.setFont(QFont("Segoe UI", 7))
        for step in (0.25, 0.5, 0.75, 1.0):
            gy = rect.y() + pad_top + usable_h * (1 - step)
            painter.setPen(color_lbl)
            painter.drawText(
                QRectF(rect.x(), gy - 8, pad_l - 10, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _fmt_currency_short(max_val * step),
            )
            painter.setPen(QPen(color_grid, 0.5, Qt.PenStyle.DashLine))
            painter.drawLine(
                QPointF(rect.x() + pad_l, gy),
                QPointF(rect.right() - pad_r, gy),
            )

        # ── Left axis line ──────────────────────────────────────────────
        painter.setPen(QPen(color_axis, 1.0, Qt.PenStyle.SolidLine))
        painter.drawLine(
            QPointF(rect.x() + pad_l, rect.y() + pad_top),
            QPointF(rect.x() + pad_l, rect.y() + pad_top + usable_h),
        )

        # ── Bars ─────────────────────────────────────────────────────
        painter.setFont(QFont("Segoe UI", 7))

        for i, (label, income, expense) in enumerate(self._data):
            gx     = rect.x() + pad_l + i * group_w
            center = gx + group_w / 2

            # Income bar
            h_inc = usable_h * income / max_val
            bx_inc = center - bar_w - gap / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color_inc))
            painter.drawRoundedRect(
                QRectF(bx_inc,
                       rect.y() + pad_top + usable_h - h_inc,
                       bar_w, h_inc),
                2, 2,
            )

            # Expense bar
            h_exp = usable_h * expense / max_val
            bx_exp = center + gap / 2
            painter.setBrush(QBrush(color_exp))
            painter.drawRoundedRect(
                QRectF(bx_exp,
                       rect.y() + pad_top + usable_h - h_exp,
                       bar_w, h_exp),
                2, 2,
            )

            # Month label
            painter.setPen(color_lbl)
            painter.drawText(
                QRectF(gx, rect.bottom() - pad_bot, group_w, pad_bot),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                label,
            )


# ---------------------------------------------------------------------------
# Paint canvas helper — delegates paintEvent to a callable
# ---------------------------------------------------------------------------

class _PaintCanvas(QWidget):
    """Minimal widget that delegates painting to an external callable."""

    def __init__(self, paint_fn) -> None:
        super().__init__()
        self._paint_fn = paint_fn
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        self._paint_fn(painter, rect)
        painter.end()


# ---------------------------------------------------------------------------
# Recent transactions mini-table
# ---------------------------------------------------------------------------

class _RecentTransactionsWidget(QFrame):
    """Last 5 transactions — compact table, no pagination."""

    COLS = ["Fecha", "Emisor / Concepto", "Importe", "Categoría"]

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self._t = theme
        self.setObjectName("chartCard")
        self.setMinimumHeight(200)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(8)

        lbl = QLabel("Últimas transacciones")
        lbl.setObjectName("chartTitle")
        root.addWidget(lbl)

        self._table = QTableWidget(0, len(self.COLS))
        self._table.setHorizontalHeaderLabels(self.COLS)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setObjectName("recentTable")

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        root.addWidget(self._table)
        apply_card_shadow(self, self._t)

    def set_data(self, rows: List[dict]) -> None:
        """
        rows: list of dicts with keys:
          transaction_date, issuer_name, concept, amount, category_name
        """
        t = self._t
        self._table.setRowCount(0)
        for row in rows[:5]:
            r = self._table.rowCount()
            self._table.insertRow(r)

            date_str = str(row.get("transaction_date", ""))[:10]
            issuer   = (row.get("issuer_name") or
                        row.get("concept") or "—")[:40]
            amount   = float(row.get("amount") or 0.0)
            cat      = row.get("category_name") or "—"

            amt_str  = _fmt_amount(amount)
            color    = t["income"] if amount >= 0 else t["expense"]

            for col, (text, align) in enumerate([
                (date_str,  Qt.AlignmentFlag.AlignLeft),
                (issuer,    Qt.AlignmentFlag.AlignLeft),
                (amt_str,   Qt.AlignmentFlag.AlignRight),
                (cat,       Qt.AlignmentFlag.AlignLeft),
            ]):
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    align | Qt.AlignmentFlag.AlignVCenter
                )
                if col == 2:
                    item.setForeground(QColor(color))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                              Qt.ItemFlag.ItemIsSelectable)
                self._table.setItem(r, col, item)

        self._table.resizeRowsToContents()

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        apply_card_shadow(self, theme)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_currency_short(value: float) -> str:
    """€1.234 / €12,3k / €1,2M"""
    av = abs(value)
    if av >= 1_000_000:
        return f"€{av/1_000_000:.1f}M"
    if av >= 1_000:
        return f"€{av/1_000:.1f}k"
    return f"€{av:,.0f}".replace(",", ".")


def _fmt_amount(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return (
        f"{sign}{abs(value):,.2f} €"
        .replace(",", "X").replace(".", ",").replace("X", ".")
    )


# ---------------------------------------------------------------------------

class _SectionHeader(QLabel):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setObjectName("sectionHeader")


# ---------------------------------------------------------------------------
# Dashboard view
# ---------------------------------------------------------------------------

class DashboardView(QWidget):

    RANGES = [
        ("Mes actual",    "month"),
        ("Año actual",    "year"),
        ("Todo",          "all"),
    ]

    def __init__(self, user: User, session: Session,
                 theme: str = "light") -> None:
        super().__init__()
        self._user    = user
        self._session = session
        self._theme   = theme
        self._t       = THEMES[theme]
        self._range   = "month"

        self._build_ui()
        self._apply_theme()
        self.refresh_data()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Dashboard top bar ──────────────────────────────────────────
        topbar = QFrame()
        topbar.setObjectName("dashTopBar")
        topbar.setFixedHeight(56)
        tbl = QHBoxLayout(topbar)
        tbl.setContentsMargins(24, 0, 24, 0)

        lbl_title = QLabel("Posición Global")
        lbl_title.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        lbl_title.setObjectName("dashTitle")
        tbl.addWidget(lbl_title)

        tbl.addStretch()

        # Range selector
        lbl_range = QLabel("Periodo:")
        lbl_range.setObjectName("dashRangeLabel")
        tbl.addWidget(lbl_range)

        tbl.addSpacing(8)

        self._cmb_range = QComboBox()
        self._cmb_range.setFixedHeight(32)
        self._cmb_range.setObjectName("dashCombo")
        for label, key in self.RANGES:
            self._cmb_range.addItem(label, key)
        self._cmb_range.currentIndexChanged.connect(self._on_range_changed)
        tbl.addWidget(self._cmb_range)

        tbl.addSpacing(8)

        # Refresh button
        self._btn_refresh = QPushButton()
        self._btn_refresh.setFixedSize(32, 32)
        self._btn_refresh.setFlat(True)
        self._btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_refresh.setToolTip("Actualizar datos")
        self._btn_refresh.clicked.connect(self.refresh_data)
        tbl.addWidget(self._btn_refresh)

        root.addWidget(topbar)

        # ── Scrollable content ─────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName("dashScroll")

        content = QWidget()
        content.setObjectName("dashContent")
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(24, 24, 24, 24)
        self._content_layout.setSpacing(24)

        # KPI section header
        self._content_layout.addWidget(
            _SectionHeader("Resumen del periodo")
        )

        # KPI grid (3 per row)
        kpi_grid_widget = QWidget()
        self._kpi_grid = QGridLayout(kpi_grid_widget)
        self._kpi_grid.setSpacing(16)
        self._kpi_grid.setContentsMargins(0, 0, 0, 0)
        self._content_layout.addWidget(kpi_grid_widget)

        # Build KPI cards
        self._cards: dict[str, _KPICard] = {}
        kpi_defs = [
            ("balance",       "Saldo actual",        "mdi.bank-outline",
             self._t["accent"]),
            ("income",        "Ingresos",             "mdi.trending-up",
             self._t["income"]),
            ("expense",       "Gastos",               "mdi.trending-down",
             self._t["expense"]),
            ("investment",    "Inversiones",          "mdi.chart-line",
             self._t["investment"]),
            ("savings_rate",  "Tasa de ahorro",       "mdi.piggy-bank-outline",
             self._t["accent"]),
            ("uncategorized", "Sin categorizar",      "mdi.tag-off-outline",
             self._t["warning"]),
        ]
        for idx, (key, label, icon, colour) in enumerate(kpi_defs):
            card = _KPICard(label, icon, colour, self._t)
            row, col = divmod(idx, 3)
            self._kpi_grid.addWidget(card, row, col)
            self._cards[key] = card

        # Make columns equal width
        for col in range(3):
            self._kpi_grid.setColumnStretch(col, 1)

        # Charts section header
        self._content_layout.addWidget(
            _SectionHeader("Análisis visual")
        )

        # ── Chart row: category donut + monthly bars ──────────────────
        charts_row = QHBoxLayout()
        charts_row.setSpacing(16)

        self._chart_category = _CategoryChartWidget(self._t)
        self._chart_monthly  = _MonthlyChartWidget(self._t)
        charts_row.addWidget(self._chart_category)
        charts_row.addWidget(self._chart_monthly)
        self._content_layout.addLayout(charts_row)

        # ── Recent transactions ───────────────────────────────────────
        self._content_layout.addWidget(
            _SectionHeader("Últimas transacciones")
        )
        self._recent_widget = _RecentTransactionsWidget(self._t)
        self._content_layout.addWidget(self._recent_widget)

        self._content_layout.addStretch()

        scroll.setWidget(content)
        root.addWidget(scroll)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def refresh_data(self) -> None:
        """Query DB and refresh all KPI cards."""
        today      = date.today()
        date_from, date_to = self._get_date_range(today)

        summary = dashboard_summary(self._session, self._user.id, date_from, date_to)
        balance      = summary.balance
        income       = summary.income
        expense      = summary.expense
        investment   = summary.investment
        savings_rate = summary.savings_rate
        uncat        = summary.uncategorized

        self._chart_category.set_data(summary.by_category)

        month_abbr = ["", "Ene", "Feb", "Mar", "Abr", "May",
                      "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        self._chart_monthly.set_data([
            (f"{month_abbr[m.month]}\n{str(m.year)[2:]}", m.income, m.expense)
            for m in summary.monthly
        ])

        self._recent_widget.set_data([
            {
                "transaction_date": row.transaction_date,
                "issuer_name":      row.issuer_name,
                "concept":          row.concept,
                "amount":           row.amount,
                "category_name":    row.category_name,
            }
            for row in summary.recent
        ])

        # Update cards
        self._cards["balance"].set_value(
            self._fmt_currency(balance),
            sub=f"A {today.strftime('%d %b %Y')}",
        )
        self._cards["income"].set_value(
            self._fmt_currency(income),
            sub=self._period_label(date_from, date_to),
        )
        self._cards["expense"].set_value(
            self._fmt_currency(expense),
            sub=self._period_label(date_from, date_to),
        )
        self._cards["investment"].set_value(
            self._fmt_currency(investment),
            sub=self._period_label(date_from, date_to),
        )
        self._cards["savings_rate"].set_value(
            f"{savings_rate:.1f}%",
            sub="Ingreso − Gasto / Ingreso",
            badge=(f"{'↑' if savings_rate >= 0 else '↓'} "
                   f"{abs(savings_rate):.1f}%"),
            badge_positive=savings_rate >= 0,
        )
        self._cards["uncategorized"].set_value(
            str(uncat),
            sub="transacciones pendientes",
        )

    def refresh_theme(self, theme: str) -> None:
        self._theme = theme
        self._t     = THEMES[theme]
        self._apply_theme()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_date_range(
        self, today: date
    ) -> tuple[date, date]:
        return range_bounds(self._range, today)

    def _period_label(self, d_from: date, d_to: date) -> str:
        return f"{d_from.strftime('%d %b')} – {d_to.strftime('%d %b %Y')}"

    @staticmethod
    def _fmt_currency(value: float) -> str:
        return f"€{abs(value):,.2f}".replace(",", "X").replace(
            ".", ","
        ).replace("X", ".")

    def _on_range_changed(self, index: int) -> None:
        self._range = self._cmb_range.currentData()
        self.refresh_data()

    def _apply_theme(self) -> None:
        t = self._t

        self._btn_refresh.setIcon(
            qta.icon("mdi.refresh", color=t["text_secondary"])
        )

        # Re-style cards
        for card in self._cards.values():
            card.apply_style(t)

        # Re-style chart widgets
        if hasattr(self, "_chart_category"):
            self._chart_category.apply_theme(t)
        if hasattr(self, "_chart_monthly"):
            self._chart_monthly.apply_theme(t)
        if hasattr(self, "_recent_widget"):
            self._recent_widget.apply_theme(t)

        self.setStyleSheet(f"""
            QWidget#dashContent {{
                background-color: {t["bg_primary"]};
            }}
            QScrollArea#dashScroll {{
                background-color: {t["bg_primary"]};
                border: none;
            }}
            QFrame#dashTopBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLabel#dashTitle {{
                color: {t["text_primary"]};
                font-size: 16px;
                font-weight: 700;
            }}
            QLabel#dashRangeLabel {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QComboBox#dashCombo {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 6px;
                padding: 4px 10px;
                color: {t["text_primary"]};
                font-size: 12px;
                min-width: 130px;
            }}
            QComboBox#dashCombo::drop-down {{
                border: none;
            }}
            QLabel#sectionHeader {{
                color: {t["text_primary"]};
                font-size: 13px;
                font-weight: 700;
                padding-bottom: 4px;
                border-bottom: 2px solid {t["accent"]};
            }}
            QFrame#chartCard {{
                background-color: {t["bg_secondary"]};
                border-radius: 12px;
                border: 1px solid {t["border"]};
            }}
            QLabel#chartTitle {{
                color: {t["text_primary"]};
                font-size: 13px;
                font-weight: 600;
            }}
            QLabel#chartPlaceholder {{
                color: {t["neutral"]};
                font-size: 12px;
            }}
            QTableWidget#recentTable {{
                background-color: {t["bg_primary"]};
                alternate-background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                gridline-color: {t["border"]};
                font-size: 12px;
                selection-background-color: {t["accent"]};
                selection-color: #FFFFFF;
            }}
            QTableWidget#recentTable::item {{
                padding: 6px 8px;
                border: none;
            }}
            QTableWidget#recentTable::item:alternate {{
                background-color: {t["bg_secondary"]};
            }}
            QTableWidget#recentTable::item:selected {{
                background-color: {t["accent"]};
                color: #FFFFFF;
            }}
            QHeaderView::section {{
                background-color: {t["bg_hover"]};
                color: {t["text_secondary"]};
                font-size: 11px; font-weight: 600;
                border: none; padding: 5px 8px;
            }}
        """)