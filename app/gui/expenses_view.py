"""
app/gui/expenses_view.py
=========================
PROFIN — Expenses View (Gastos)

Sections:
  1. Period selector  — Mes actual / Mes anterior / Año actual / Año anterior / Todo
  2. KPI strip        — Total, nº transacciones, media, mayor gasto, % categorizado
  3. Distribution     — _CategoryChartWidget reused + Line chart toggle (3 modes)
  4. Comparison       — Grouped bars: current vs previous period by category
  5. Detail table     — Sortable, top-50, paginable
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime    import date, datetime
from typing      import Dict, List, Optional, Tuple

from PySide6.QtCore    import Qt, QRectF, QPointF
from PySide6.QtGui     import (
    QColor, QFont, QPainter, QPen, QBrush, QFontMetrics,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QPushButton, QComboBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSizePolicy, QButtonGroup,
)
import qtawesome as qta

from sqlalchemy     import func, extract
from sqlalchemy.orm import Session

from app.config    import THEMES
from app.db.models import Transaction, TransactionType, Category, User

# Reuse chart palette and helpers from dashboard
from app.gui.dashboard import (
    _CHART_PALETTE, _CHART_COLOR_OVERRIDES,
    _chart_color, _CategoryChartWidget,
    _PaintCanvas, _fmt_currency_short, _fmt_amount,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Comparison chart — grouped bars: current vs previous period by category
# ---------------------------------------------------------------------------

class _ComparisonChartWidget(QFrame):
    """
    Grouped bar chart comparing expenses by category across two periods.
    data_a = current period, data_b = comparison period.
    """

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self._t      = theme
        self._data_a: List[Tuple[str, float]] = []   # (label, value) current
        self._data_b: List[Tuple[str, float]] = []   # (label, value) previous
        self._label_a = "Período actual"
        self._label_b = "Período anterior"

        self.setObjectName("chartCard")
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(8)

        # Header + legend
        hdr = QHBoxLayout()
        self._lbl_title = QLabel("Comparativa por categoría")
        self._lbl_title.setObjectName("chartTitle")
        hdr.addWidget(self._lbl_title)
        hdr.addStretch()
        for attr, color, text in [
            ("_dot_a", "#2563EB", ""),
            ("_lbl_a", "#2563EB", "Actual"),
            ("_dot_b", "#94A3B8", ""),
            ("_lbl_b", "#94A3B8", "Anterior"),
        ]:
            w = QLabel("●" if "dot" in attr else text)
            w.setStyleSheet(f"color: {color}; font-size: 11px;")
            hdr.addWidget(w)
            hdr.addSpacing(2)
        root.addLayout(hdr)

        self._canvas = _PaintCanvas(self._paint_chart)
        self._canvas.setMinimumHeight(220)
        root.addWidget(self._canvas)

    def set_data(
        self,
        data_a: List[Tuple[str, float]],
        data_b: List[Tuple[str, float]],
        label_a: str = "Período actual",
        label_b: str = "Período anterior",
    ) -> None:
        # Align both lists to same category set
        cats = list({c for c, _ in data_a} | {c for c, _ in data_b})
        cats.sort()
        map_a = dict(data_a)
        map_b = dict(data_b)
        self._data_a  = [(c, map_a.get(c, 0.0)) for c in cats]
        self._data_b  = [(c, map_b.get(c, 0.0)) for c in cats]
        self._label_a = label_a
        self._label_b = label_b
        self._canvas.update()

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        self._canvas.update()

    def _paint_chart(self, painter: QPainter, rect: QRectF) -> None:
        if not self._data_a and not self._data_b:
            painter.setPen(QColor(self._t.get("text_secondary", "#888")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Sin datos para comparar")
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        n        = max(len(self._data_a), len(self._data_b))
        if n == 0:
            return

        pad_l    = 12
        pad_r    = 8
        pad_top  = 8
        pad_bot  = 28
        usable_w = rect.width()  - pad_l - pad_r
        usable_h = rect.height() - pad_top - pad_bot

        group_w  = usable_w / n
        bar_w    = max(4.0, group_w * 0.30)
        gap      = max(1.0, group_w * 0.04)

        all_vals = [v for _, v in self._data_a] + [v for _, v in self._data_b]
        max_val  = max(all_vals) if all_vals else 1.0
        max_val  = max_val or 1.0

        color_a   = QColor("#2563EB")
        color_b   = QColor("#94A3B8")
        color_lbl = QColor(self._t.get("text_secondary", "#888"))
        color_grid = QColor(self._t.get("border", "#ddd"))

        # Gridlines
        painter.setPen(QPen(color_grid, 0.5, Qt.PenStyle.DashLine))
        for step in (0.25, 0.5, 0.75, 1.0):
            gy = rect.y() + pad_top + usable_h * (1 - step)
            painter.drawLine(
                QPointF(rect.x() + pad_l, gy),
                QPointF(rect.right() - pad_r, gy),
            )

        painter.setFont(QFont("Segoe UI", 7))

        for i in range(n):
            label   = self._data_a[i][0] if i < len(self._data_a) else self._data_b[i][0]
            val_a   = self._data_a[i][1] if i < len(self._data_a) else 0.0
            val_b   = self._data_b[i][1] if i < len(self._data_b) else 0.0

            gx      = rect.x() + pad_l + i * group_w
            center  = gx + group_w / 2

            # Bar A (current — blue)
            h_a  = usable_h * val_a / max_val
            bx_a = center - bar_w - gap / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color_a))
            painter.drawRoundedRect(
                QRectF(bx_a, rect.y() + pad_top + usable_h - h_a, bar_w, h_a),
                2, 2,
            )

            # Bar B (previous — grey)
            h_b  = usable_h * val_b / max_val
            bx_b = center + gap / 2
            painter.setBrush(QBrush(color_b))
            painter.drawRoundedRect(
                QRectF(bx_b, rect.y() + pad_top + usable_h - h_b, bar_w, h_b),
                2, 2,
            )

            # Category label (rotated or truncated)
            painter.setPen(color_lbl)
            short = label[:10] + "…" if len(label) > 10 else label
            painter.drawText(
                QRectF(gx, rect.bottom() - pad_bot, group_w, pad_bot),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                short,
            )


# ---------------------------------------------------------------------------
# Monthly top-category chart — stacked bars by month
# ---------------------------------------------------------------------------

class _MonthlyCategoryChartWidget(QFrame):
    """Stacked monthly bars showing the main expense categories in the selected period."""

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self._t = theme
        self._data: List[Tuple[str, List[Tuple[str, float]]]] = []

        self.setObjectName("chartCard")
        self.setMinimumHeight(250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(8)

        hdr = QHBoxLayout()
        self._lbl_title = QLabel("Top categorías por mes")
        self._lbl_title.setObjectName("chartTitle")
        hdr.addWidget(self._lbl_title)
        hdr.addStretch()
        root.addLayout(hdr)

        self._canvas = _PaintCanvas(self._paint_chart)
        self._canvas.setMinimumHeight(180)
        root.addWidget(self._canvas)

    def set_data(self, data: List[Tuple[str, List[Tuple[str, float]]]]) -> None:
        self._data = data
        self._canvas.update()

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        self._canvas.update()

    def _paint_chart(self, painter: QPainter, rect: QRectF) -> None:
        if not self._data:
            painter.setPen(QColor(self._t.get("text_secondary", "#888")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Sin datos por mes")
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        n = len(self._data)
        pad_l = 16
        pad_r = 12
        pad_top = 10
        pad_bot = 28
        usable_w = rect.width() - pad_l - pad_r
        usable_h = rect.height() - pad_top - pad_bot
        group_w = usable_w / max(n, 1)
        bar_w = max(18.0, group_w * 0.72)
        max_total = max(sum(value for _, value in cats) for _, cats in self._data) if self._data else 1.0
        max_total = max_total or 1.0
        color_grid = QColor(self._t.get("border", "#ddd"))
        color_lbl = QColor(self._t.get("text_secondary", "#888"))

        painter.setPen(QPen(color_grid, 0.5, Qt.PenStyle.DashLine))
        for step in (0.25, 0.5, 0.75, 1.0):
            gy = rect.y() + pad_top + usable_h * (1 - step)
            painter.drawLine(QPointF(rect.x() + pad_l, gy), QPointF(rect.right() - pad_r, gy))

        for i, (label, cats) in enumerate(self._data):
            total = sum(value for _, value in cats)
            month_x = rect.x() + pad_l + i * group_w + (group_w - bar_w) / 2
            h = usable_h * total / max_total
            base_y = rect.y() + pad_top + usable_h
            covered = 0.0
            stack_total = total or 1.0

            for idx, (cat_name, value) in enumerate(cats):
                seg_h = max(0.0, h * (value / stack_total))
                if seg_h <= 0:
                    continue
                painter.setBrush(QBrush(_chart_color(idx, cat_name)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(
                    QRectF(month_x, base_y - covered - seg_h, bar_w, seg_h)
                )
                covered += seg_h

            painter.setPen(color_lbl)
            painter.setFont(QFont("Segoe UI", 7))
            painter.drawText(
                QRectF(month_x, rect.bottom() - pad_bot, bar_w, pad_bot),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

# ---------------------------------------------------------------------------
# Line chart — expenses over time (daily aggregated)
# ---------------------------------------------------------------------------

class _LineChartWidget(QFrame):
    """
    Daily expense line chart for the selected period.
    """

    def __init__(self, theme: dict) -> None:
        super().__init__()
        self._t    = theme
        self._data: List[Tuple[str, float]] = []   # (day_label, total)

        self.setObjectName("chartCard")
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(8)

        hdr = QHBoxLayout()
        self._lbl_title = QLabel("Evolución diaria")
        self._lbl_title.setObjectName("chartTitle")
        hdr.addWidget(self._lbl_title)
        hdr.addStretch()
        root.addLayout(hdr)

        self._canvas = _PaintCanvas(self._paint_chart)
        self._canvas.setMinimumHeight(200)
        root.addWidget(self._canvas)

    def set_title(self, title: str) -> None:
        self._lbl_title.setText(title)

    def set_data(self, data: List[Tuple[str, float]]) -> None:
        self._data = data
        self._canvas.update()

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        self._canvas.update()

    def _paint_chart(self, painter: QPainter, rect: QRectF) -> None:
        if not self._data:
            painter.setPen(QColor(self._t.get("text_secondary", "#888")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                            "Sin datos para el período")
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pad_l   = 52
        pad_r   = 10
        pad_top = 12
        pad_bot = 24
        uw      = rect.width() - pad_l - pad_r
        uh      = rect.height() - pad_top - pad_bot

        max_val = max((v for _, v in self._data), default=1.0) or 1.0
        n       = len(self._data)

        color_line = QColor(self._t.get("accent", "#2563EB"))
        color_fill = QColor(self._t.get("accent", "#2563EB"))
        color_fill.setAlpha(30)
        color_grid = QColor(self._t.get("border", "#ddd"))
        color_lbl  = QColor(self._t.get("text_secondary", "#888"))

        painter.setFont(QFont("Segoe UI", 7))
        painter.setPen(color_lbl)
        for step in range(5):
            ratio = step / 4
            value = max_val * (1.0 - ratio)
            y = rect.y() + pad_top + uh * ratio
            painter.drawText(
                QRectF(rect.x() + 4, y - 8, pad_l - 10, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _fmt_currency_short(value),
            )
            painter.setPen(QPen(color_grid, 0.5, Qt.PenStyle.DashLine))
            painter.drawLine(
                QPointF(rect.x() + pad_l, y),
                QPointF(rect.right() - pad_r, y),
            )
            painter.setPen(color_lbl)

        # Build points
        pts: List[QPointF] = []
        for i, (_, val) in enumerate(self._data):
            x = rect.x() + pad_l + uw * i / max(n - 1, 1)
            y = rect.y() + pad_top + uh * (1 - val / max_val)
            pts.append(QPointF(x, y))

        # Fill area under the curve
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.moveTo(pts[0].x(), rect.y() + pad_top + uh)
        for p in pts:
            path.lineTo(p)
        path.lineTo(pts[-1].x(), rect.y() + pad_top + uh)
        path.closeSubpath()
        painter.setBrush(QBrush(color_fill))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(path)

        # Line
        painter.setPen(QPen(color_line, 2, Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(len(pts) - 1):
            painter.drawLine(pts[i], pts[i + 1])

        # Dots on data points (only if few points)
        if n <= 31:
            painter.setBrush(QBrush(color_line))
            painter.setPen(Qt.PenStyle.NoPen)
            for p in pts:
                painter.drawEllipse(p, 3, 3)

        axis_x = rect.x() + pad_l
        axis_y = rect.y() + pad_top
        axis_bottom = rect.y() + pad_top + uh
        axis_right = rect.right() - pad_r
        painter.setPen(QPen(QColor(self._t.get("border_strong", "#9098A3")), 1.0, Qt.PenStyle.SolidLine))
        painter.drawLine(QPointF(axis_x, axis_y), QPointF(axis_x, axis_bottom))
        painter.drawLine(QPointF(axis_x, axis_bottom), QPointF(axis_right, axis_bottom))

        painter.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        painter.setPen(color_lbl)
        painter.drawText(
            QRectF(rect.x() + 4, rect.y() + 8, 24, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "€",
        )

        # X labels (show max 8 evenly spaced)
        painter.setFont(QFont("Segoe UI", 7))
        step_lbl = max(1, n // 8)
        for i, (label, _) in enumerate(self._data):
            if i % step_lbl == 0 or i == n - 1:
                x = rect.x() + pad_l + uw * i / max(n - 1, 1)
                painter.drawText(
                    QRectF(x - 20, rect.bottom() - pad_bot, 40, pad_bot),
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    label,
                )


# ---------------------------------------------------------------------------
# KPI strip — compact horizontal row of metric chips
# ---------------------------------------------------------------------------

class _ExpenseKPI(QFrame):
    """Compact KPI chip: label + value (+ optional delta)."""

    def __init__(self, label: str, theme: dict) -> None:
        super().__init__()
        self._t = theme
        self.setObjectName("kpiChip")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(4)

        self._lbl = QLabel(label)
        self._lbl.setObjectName("kpiChipLabel")

        self._val = QLabel("—")
        self._val.setObjectName("kpiChipValue")

        self._delta = QLabel("")
        self._delta.setObjectName("kpiChipDelta")
        self._delta.setVisible(False)

        lay.addWidget(self._lbl)
        lay.addWidget(self._val)
        lay.addWidget(self._delta)

        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set_value(self, value: str,
                  delta: Optional[str] = None,
                  delta_good: bool = True) -> None:
        self._val.setText(value)
        if delta:
            self._delta.setText(delta)
            color = self._t.get("income" if delta_good else "expense", "#888")
            self._delta.setStyleSheet(f"color: {color};")
            self._delta.setVisible(True)
        else:
            self._delta.setVisible(False)

    def apply_theme(self, t: dict) -> None:
        self._t = t


# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------

# Period options: (label, months_back_start, months_back_end)
_PERIODS = [
    ("Mes actual",       0,  0),
    ("Mes anterior",     1,  1),
    ("Últimos 3 meses",  2,  0),
    ("Últimos 6 meses",  5,  0),
    ("Año actual",      -1, -1),   # special: Jan 1 of current year
    ("Año anterior",    -2, -2),   # special: full previous year
    ("Todo",            -9, -9),   # special: all data
]


def _period_dates(option_idx: int) -> Tuple[date, date]:
    """Return (date_from, date_to) for the selected period index."""
    today  = date.today()
    _, code_s, code_e = _PERIODS[option_idx]

    if code_s == -9:    # All
        return date(2000, 1, 1), today

    if code_s == -1:    # Año actual
        return date(today.year, 1, 1), today

    if code_s == -2:    # Año anterior
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)

    def _month_start(months_back: int) -> date:
        m = today.month - months_back
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        return date(y, m, 1)

    def _month_end(d: date) -> date:
        import calendar
        return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])

    d_from = _month_start(code_s)
    if code_e == code_s:
        d_to = _month_end(d_from)
    else:
        d_to = today
    return d_from, d_to


def _prev_period(date_from: date, date_to: date) -> Tuple[date, date]:
    """Return a comparison period of identical length ending just before date_from."""
    span = (date_to - date_from).days + 1
    prev_to   = date_from - __import__("datetime").timedelta(days=1)
    prev_from = prev_to   - __import__("datetime").timedelta(days=span - 1)
    return prev_from, prev_to


class ExpensesView(QWidget):
    """
    Expenses analysis view — distribution charts, comparison, detail table.
    Auto-refreshes on showEvent (same pattern as TransactionsView).
    """

    CHART_DIST   = "dist"
    CHART_COMP   = "comp"
    CHART_LINE   = "line"
    CHART_TABLE  = "table"
    TRANSACTION_TYPE = TransactionType.expense
    VIEW_TITLE = "Gastos"
    TOTAL_LABEL = "Total gastos"
    MAX_LABEL = "Mayor gasto"
    TABLE_OBJECT_NAME = "expenseTable"
    AMOUNT_COLOR_KEY = "expense"
    AMOUNT_COLOR_FALLBACK = "#DC2626"
    LINE_TITLE = "Evolución diaria de gastos"
    LOG_NAME = "ExpensesView"
    DETAIL_HEADER = "Detalle de transacciones"

    def __init__(self, session: Session, user, theme_key: str) -> None:
        super().__init__()
        self._session   = session
        self._user      = user
        self._theme_key = theme_key
        self._t         = THEMES[theme_key]
        self._period_idx = 0           # default: Mes actual
        self._chart_mode = self.CHART_DIST

        self._build_ui()
        self._apply_theme()

    # ------------------------------------------------------------------
    # Qt lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """Auto-refresh every time this view becomes visible."""
        super().showEvent(event)
        self._load_data()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ───────────────────────────────────────────────────
        top = QFrame()
        top.setObjectName("dashTopBar")
        top.setFixedHeight(52)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(24, 0, 24, 0)
        tl.setSpacing(12)

        lbl_title = QLabel(self.VIEW_TITLE)
        lbl_title.setObjectName("dashTitle")
        tl.addWidget(lbl_title)

        tl.addSpacing(24)

        lbl_per = QLabel("Período:")
        lbl_per.setObjectName("filterLabel")
        tl.addWidget(lbl_per)

        self._cmb_period = QComboBox()
        self._cmb_period.setFixedHeight(32)
        self._cmb_period.setMinimumWidth(160)
        for label, _, _ in _PERIODS:
            self._cmb_period.addItem(label)
        self._cmb_period.currentIndexChanged.connect(self._on_period_changed)
        tl.addWidget(self._cmb_period)

        tl.addStretch()

        # Chart mode toggle buttons
        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)
        for mode, icon_name, tip in [
            (self.CHART_DIST, "mdi.chart-donut",   "Distribución por categoría"),
            (self.CHART_COMP, "mdi.chart-bar",      "Comparativa período anterior"),
            (self.CHART_LINE, "mdi.chart-line",     "Evolución diaria"),
            (self.CHART_TABLE, "mdi.table-large",   "Vista en tabla"),
        ]:
            btn = QPushButton()
            btn.setFixedSize(32, 32)
            btn.setCheckable(True)
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setProperty("chart_mode", mode)
            btn.setObjectName("chartToggleBtn")
            self._btn_group.addButton(btn)
            tl.addWidget(btn)
            if mode == self.CHART_DIST:
                btn.setChecked(True)
                self._active_toggle_btn = btn

        self._btn_group.buttonClicked.connect(self._on_chart_toggle)

        root.addWidget(top)

        # ── Scroll area ───────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        self._content = QVBoxLayout(content)
        self._content.setContentsMargins(24, 20, 24, 24)
        self._content.setSpacing(16)

        # KPI strip
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)
        self._kpis: Dict[str, _ExpenseKPI] = {}
        for key, label in [
            ("total",     self.TOTAL_LABEL),
            ("count",     "Nº transacciones"),
            ("avg",       "Media / transacción"),
            ("max_txn",   self.MAX_LABEL),
            ("pct_cat",   "% categorizado"),
        ]:
            chip = _ExpenseKPI(label, self._t)
            self._kpis[key] = chip
            kpi_row.addWidget(chip)
        self._content.addLayout(kpi_row)

        # Distribution chart (category donut/hbar)
        self._chart_dist = _CategoryChartWidget(self._t)
        self._chart_dist.setMinimumHeight(300)
        self._content.addWidget(self._chart_dist)

        # Comparison chart
        self._chart_comp = _ComparisonChartWidget(self._t)
        self._content.addWidget(self._chart_comp)

        # Monthly category chart
        self._chart_monthly_cat = _MonthlyCategoryChartWidget(self._t)
        self._content.addWidget(self._chart_monthly_cat)

        # Line chart
        self._chart_line = _LineChartWidget(self._t)
        self._content.addWidget(self._chart_line)

        # Detail table header
        tbl_hdr = QHBoxLayout()
        lbl_tbl = QLabel(self.DETAIL_HEADER)
        lbl_tbl.setObjectName("sectionHeader")
        tbl_hdr.addWidget(lbl_tbl)
        tbl_hdr.addStretch()
        self._lbl_count = QLabel("")
        self._lbl_count.setObjectName("txnCount")
        tbl_hdr.addWidget(self._lbl_count)
        self._content.addLayout(tbl_hdr)

        # Detail table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Fecha", "Emisor / Concepto", "Categoría", "Importe"]
        )
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setObjectName(self.TABLE_OBJECT_NAME)
        self._table.setSortingEnabled(True)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Expanding)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self._content.addWidget(self._table, 1)

        scroll.setWidget(content)
        root.addWidget(scroll)

        # Initial chart visibility
        self._refresh_chart_visibility()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_period_changed(self, idx: int) -> None:
        self._period_idx = idx
        self._load_data()

    def _on_chart_toggle(self, btn: QPushButton) -> None:
        self._chart_mode = btn.property("chart_mode")
        self._refresh_chart_visibility()
        self._update_toggle_icons()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_data(self) -> None:
        date_from, date_to = _period_dates(self._period_idx)
        prev_from, prev_to = _prev_period(date_from, date_to)

        period_label   = _PERIODS[self._period_idx][0]
        logger.debug("ExpensesView: cargando período %s (%s → %s)",
                     period_label, date_from, date_to)

        try:
            # ── Base query helper ─────────────────────────────────────
            def _expense_q(d_from: date, d_to: date):
                return (
                    self._session.query(Transaction)
                    .filter(
                        Transaction.user_id          == self._user.id,
                        Transaction.type             == self.TRANSACTION_TYPE,
                        Transaction.transaction_date >= d_from,
                        Transaction.transaction_date <= d_to,
                    )
                )

            txns_cur  = _expense_q(date_from, date_to).all()
            txns_prev = _expense_q(prev_from, prev_to).all()

            # ── KPIs ──────────────────────────────────────────────────
            amounts_cur  = [abs(t.amount) for t in txns_cur]
            total_cur    = sum(amounts_cur)
            count_cur    = len(amounts_cur)
            avg_cur      = total_cur / count_cur if count_cur else 0.0
            max_cur      = max(amounts_cur) if amounts_cur else 0.0
            cat_cur      = sum(1 for t in txns_cur if t.category_id)
            pct_cat      = cat_cur / count_cur * 100 if count_cur else 0.0

            total_prev   = sum(abs(t.amount) for t in txns_prev)

            # Delta vs previous period
            if total_prev > 0:
                delta_pct = (total_cur - total_prev) / total_prev * 100
                delta_str = f"{'▲' if delta_pct > 0 else '▼'} {abs(delta_pct):.1f}% vs anterior"
                delta_good = delta_pct < 0   # less spending = good
            else:
                delta_str = None
                delta_good = True

            self._kpis["total"].set_value(
                _fmt_amount(total_cur), delta_str, delta_good
            )
            self._kpis["count"].set_value(
                str(count_cur),
                f"{'▲' if len(txns_prev) and count_cur > len(txns_prev) else '▼'} "
                f"{abs(count_cur - len(txns_prev))} vs anterior"
                if txns_prev else None,
                count_cur <= len(txns_prev),
            )
            self._kpis["avg"].set_value(_fmt_amount(avg_cur))
            self._kpis["max_txn"].set_value(_fmt_amount(max_cur))
            self._kpis["pct_cat"].set_value(f"{pct_cat:.1f}%",
                                            None if pct_cat >= 80 else "⚠ Pendientes")

            # ── Category map ──────────────────────────────────────────
            cat_map = {
                c.id: c.name
                for c in self._session.query(Category)
                .filter(Category.user_id == self._user.id).all()
            }

            # ── Distribution chart data ───────────────────────────────
            cat_totals: Dict[str, float] = defaultdict(float)
            for t in txns_cur:
                label = cat_map.get(t.category_id, "Sin categorizar")
                cat_totals[label] += abs(t.amount)

            dist_data = sorted(cat_totals.items(), key=lambda x: x[1],
                               reverse=True)
            self._chart_dist.set_data(dist_data[:10])

            # ── Comparison chart data ─────────────────────────────────
            cat_prev: Dict[str, float] = defaultdict(float)
            for t in txns_prev:
                label = cat_map.get(t.category_id, "Sin categorizar")
                cat_prev[label] += abs(t.amount)

            self._chart_comp.set_data(
                dist_data,
                sorted(cat_prev.items(), key=lambda x: x[1], reverse=True),
                label_a=period_label,
                label_b="Período anterior",
            )

            # ── Monthly category breakdown ─────────────────────────────
            month_totals: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
            for t in txns_cur:
                month_key = t.transaction_date.strftime("%b %Y")
                label = cat_map.get(t.category_id, "Sin categorizar")
                month_totals[month_key][label] += abs(t.amount)

            monthly_cat_data: List[Tuple[str, List[Tuple[str, float]]]] = []
            for month_key in sorted(month_totals.keys(), key=lambda item: datetime.strptime(item, "%b %Y")):
                cat_items = sorted(month_totals[month_key].items(), key=lambda x: x[1], reverse=True)[:4]
                monthly_cat_data.append((month_key, cat_items))
            self._chart_monthly_cat.set_data(monthly_cat_data)

            # ── Line chart — daily aggregation ────────────────────────
            daily: Dict[str, float] = defaultdict(float)
            for t in txns_cur:
                key = t.transaction_date.strftime("%d/%m")
                daily[key] += abs(t.amount)

            line_data = sorted(daily.items())
            self._chart_line.set_title(f"{self.LINE_TITLE} · {period_label}")
            self._chart_line.set_data(line_data)

            # ── Detail table ──────────────────────────────────────────
            self._table.setSortingEnabled(False)
            self._table.setRowCount(0)
            txns_sorted = sorted(txns_cur,
                                 key=lambda t: t.transaction_date,
                                 reverse=True)[:50]

            for txn in txns_sorted:
                r = self._table.rowCount()
                self._table.insertRow(r)

                date_str = str(txn.transaction_date)[:10]
                issuer   = (txn.issuer_name or txn.concept or "—")[:50]
                cat      = cat_map.get(txn.category_id, "—")
                amt_str  = _fmt_amount(abs(txn.amount))

                for col, (text, align) in enumerate([
                    (date_str, Qt.AlignmentFlag.AlignLeft),
                    (issuer,   Qt.AlignmentFlag.AlignLeft),
                    (cat,      Qt.AlignmentFlag.AlignLeft),
                    (amt_str,  Qt.AlignmentFlag.AlignRight),
                ]):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(
                        align | Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                                  Qt.ItemFlag.ItemIsSelectable)
                    if col == 3:
                        item.setForeground(
                            QColor(                            self._t.get(self.AMOUNT_COLOR_KEY, self.AMOUNT_COLOR_FALLBACK))
                        )
                    self._table.setItem(r, col, item)

            self._table.setSortingEnabled(True)
            self._table.resizeRowsToContents()
            self._lbl_count.setText(
                f"{count_cur} transacciones  •  {date_from} → {date_to}"
            )

            logger.debug(
                "ExpensesView: %d transacciones cargadas, total=%.2f€",
                count_cur, total_cur,
            )

        except Exception as exc:
            logger.exception("ExpensesView._load_data error: %s", exc)

    # ------------------------------------------------------------------
    # Chart toggle helpers
    # ------------------------------------------------------------------

    def _refresh_chart_visibility(self) -> None:
        self._chart_dist.setVisible(self._chart_mode == self.CHART_DIST)
        self._chart_comp.setVisible(self._chart_mode == self.CHART_COMP)
        self._chart_line.setVisible(self._chart_mode == self.CHART_LINE)
        self._table.setVisible(self._chart_mode == self.CHART_TABLE)

    def _update_toggle_icons(self) -> None:
        for btn in self._btn_group.buttons():
            mode   = btn.property("chart_mode")
            active = (mode == self._chart_mode)
            color  = self._t.get("accent" if active else "text_secondary",
                                 "#888")
            icon_map = {
                self.CHART_DIST: "mdi.chart-donut",
                self.CHART_COMP: "mdi.chart-bar",
                self.CHART_LINE: "mdi.chart-line",
                self.CHART_TABLE: "mdi.table-large",
            }
            btn.setIcon(qta.icon(icon_map[mode], color=color))

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def apply_theme(self, theme_key: str) -> None:
        self._theme_key = theme_key
        self._t         = THEMES[theme_key]
        self._apply_theme()

    def _apply_theme(self) -> None:
        t = self._t

        # Refresh toggle icons
        self._update_toggle_icons()

        # Refresh charts
        if hasattr(self, "_chart_dist"):
            self._chart_dist.apply_theme(t)
        if hasattr(self, "_chart_comp"):
            self._chart_comp.apply_theme(t)
        if hasattr(self, "_chart_monthly_cat"):
            self._chart_monthly_cat.apply_theme(t)
        if hasattr(self, "_chart_line"):
            self._chart_line.apply_theme(t)
        for chip in self._kpis.values():
            chip.apply_theme(t)

        self.setStyleSheet(f"""
            QWidget {{
                background-color: {t["bg_primary"]};
                color: {t["text_primary"]};
                font-family: "Segoe UI";
                font-size: 13px;
            }}
            QFrame#dashTopBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLabel#dashTitle {{
                color: {t["text_primary"]};
                font-size: 16px; font-weight: 700;
            }}
            QLabel#filterLabel {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QLabel#txnCount {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QLabel#sectionHeader {{
                color: {t["text_secondary"]};
                font-size: 11px; font-weight: 600;
                text-transform: uppercase; letter-spacing: 1px;
            }}
            QFrame#kpiChip {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
            }}
            QLabel#kpiChipLabel {{
                color: {t["text_secondary"]};
                font-size: 11px;
            }}
            QLabel#kpiChipValue {{
                color: {t["text_primary"]};
                font-size: 18px; font-weight: 700;
            }}
            QLabel#kpiChipDelta {{
                font-size: 11px;
            }}
            QFrame#chartCard {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 10px;
            }}
            QLabel#chartTitle {{
                color: {t["text_primary"]};
                font-size: 13px; font-weight: 600;
            }}
            QPushButton#chartToggleBtn {{
                border: none;
                border-radius: 6px;
                background-color: transparent;
            }}
            QPushButton#chartToggleBtn:checked {{
                background-color: {t["bg_hover"]};
            }}
            QPushButton#chartToggleBtn:hover {{
                background-color: {t["bg_hover"]};
            }}
            QComboBox {{
                background-color: {t["bg_secondary"]};
                color: {t["text_primary"]};
                border: 1px solid {t["border"]};
                border-radius: 6px;
                padding: 0 10px;
            }}
            QTableWidget#{self.TABLE_OBJECT_NAME} {{
                background-color: {t["bg_primary"]};
                alternate-background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                gridline-color: {t["border"]};
                font-size: 12px;
                selection-background-color: {t["accent"]};
                selection-color: #FFFFFF;
            }}
            QTableWidget#{self.TABLE_OBJECT_NAME}::item {{
                padding: 6px 8px;
                border: none;
            }}
            QTableWidget#{self.TABLE_OBJECT_NAME}::item:alternate {{
                background-color: {t["bg_secondary"]};
            }}
            QTableWidget#{self.TABLE_OBJECT_NAME}::item:selected {{
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