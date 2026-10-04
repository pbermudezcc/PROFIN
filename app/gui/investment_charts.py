"""
app/gui/investment_charts.py
============================
FAMILYFIN — "Evolución" and "Distribución" tabs of the Inversiones view.

EvolutionPanel
    Line chart of the portfolio value against the FIFO cost of what is held
    ("Capital invertido") and, optionally, a benchmark fed with the same cash
    flows. Period buttons (1M · 6M · Año · 1A · 3A · Todo), benchmark
    selector, and a hover read-out (crosshair + box with the day's values).
    Painted with QPainter like the dashboard charts: no chart library.

AllocationPanel
    The dashboard's donut/bars card (_CategoryChartWidget) fed with the
    current market value grouped by security, broker, currency or type.

Both only read the local cache (performance.py); the view reloads them
lazily, when their tab is visible.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.gui._format import fmt_date, fmt_eur, fmt_number
from app.gui._style_utils import apply_card_shadow
from app.gui.dashboard import _CategoryChartWidget
from app.investments.benchmarks import BENCHMARKS, DEFAULT_BENCHMARK
from app.investments.performance import (
    ALLOCATION_DIMENSIONS,
    HistoryPoint,
    PortfolioHistory,
    allocation,
    portfolio_history,
)

_MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
BENCHMARK_SETTING = "investments/benchmark"
BENCHMARK_COLOR = "#D97706"

PERIODS = [("1M", 31), ("6M", 183), ("Año", "ytd"), ("1A", 365), ("3A", 3 * 365), ("Todo", None)]


def _short_eur(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{fmt_number(value / 1_000_000, 1)} M€"
    if abs(value) >= 10_000:
        return f"{fmt_number(value / 1000, 0)} k€"
    if abs(value) >= 1000:
        return f"{fmt_number(value / 1000, 1)} k€"
    return f"{fmt_number(value, 0)} €"


class _HoverCanvas(QWidget):
    """Paints through a callable and remembers the mouse x for a crosshair."""

    def __init__(self, paint_fn) -> None:
        super().__init__()
        self._paint_fn = paint_fn
        self.hover_x: Optional[float] = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(300)

    def mouseMoveEvent(self, event) -> None:
        self.hover_x = event.position().x()
        self.update()

    def leaveEvent(self, event) -> None:
        self.hover_x = None
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_fn(painter, QRectF(self.rect()))
        painter.end()


# ---------------------------------------------------------------------------
# Evolution
# ---------------------------------------------------------------------------
class EvolutionPanel(QWidget):
    benchmark_changed = Signal(object)      # benchmark key or None

    def __init__(self, session: Session, user_id: int, theme: dict) -> None:
        super().__init__()
        self._session = session
        self._user_id = user_id
        self._t = theme
        self._history = PortfolioHistory()
        self._points: List[HistoryPoint] = []
        self._period = "Todo"
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)

        self._card = QFrame()
        self._card.setObjectName("chartCard")
        card = QVBoxLayout(self._card)
        card.setContentsMargins(20, 14, 20, 14)
        card.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Evolución de la cartera")
        title.setObjectName("chartTitle")
        header.addWidget(title)
        header.addStretch()
        self._period_group = QButtonGroup(self)
        self._period_group.setExclusive(True)
        for label, _days in PERIODS:
            button = QPushButton(label)
            button.setObjectName("periodButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(28)
            button.setChecked(label == self._period)
            button.clicked.connect(lambda _=False, p=label: self._set_period(p))
            self._period_group.addButton(button)
            header.addWidget(button)
        header.addSpacing(12)
        self._cmb_benchmark = QComboBox()
        self._cmb_benchmark.setObjectName("filterCombo")
        self._cmb_benchmark.setFixedHeight(30)
        self._cmb_benchmark.addItem("Sin índice de referencia", None)
        for key, bench in BENCHMARKS.items():
            self._cmb_benchmark.addItem(f"Comparar con {bench.name}", key)
        saved = QSettings("FAMILYFIN", "FAMILYFIN").value(BENCHMARK_SETTING, DEFAULT_BENCHMARK)
        index = self._cmb_benchmark.findData(saved or None)
        self._cmb_benchmark.setCurrentIndex(max(index, 0))
        self._cmb_benchmark.currentIndexChanged.connect(self._on_benchmark_changed)
        header.addWidget(self._cmb_benchmark)
        card.addLayout(header)

        self._legend = QHBoxLayout()
        self._legend.setSpacing(6)
        card.addLayout(self._legend)

        self._canvas = _HoverCanvas(self._paint)
        card.addWidget(self._canvas, 1)

        self._lbl_summary = QLabel("")
        self._lbl_summary.setObjectName("chartSummary")
        self._lbl_summary.setWordWrap(True)
        card.addWidget(self._lbl_summary)
        root.addWidget(self._card, 1)

    # -- data --------------------------------------------------------------
    @property
    def benchmark_key(self) -> Optional[str]:
        return self._cmb_benchmark.currentData()

    def reload(self, broker: Optional[str] = None) -> None:
        self._history = portfolio_history(self._session, self._user_id, broker=broker,
                                          benchmark_key=self.benchmark_key)
        self._apply_period()

    def _on_benchmark_changed(self, *_args) -> None:
        QSettings("FAMILYFIN", "FAMILYFIN").setValue(BENCHMARK_SETTING, self.benchmark_key or "")
        self.benchmark_changed.emit(self.benchmark_key)

    def _set_period(self, period: str) -> None:
        self._period = period
        self._apply_period()

    def _period_start(self) -> Optional[date]:
        points = self._history.points
        if not points:
            return None
        end = points[-1].day
        days = dict(PERIODS)[self._period]
        if days is None:
            return None
        if days == "ytd":
            return date(end.year, 1, 1)
        return end - timedelta(days=days)

    def _apply_period(self) -> None:
        self._points = self._history.since(self._period_start())
        self._rebuild_legend()
        self._update_summary()
        self._canvas.update()

    def _rebuild_legend(self) -> None:
        while self._legend.count():
            item = self._legend.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        entries = [(self._t["investment"], "Valor de la cartera", "●"),
                   (self._t["text_secondary"], "Capital invertido", "┅")]
        if self._has_benchmark():
            entries.append((BENCHMARK_COLOR, f"Mismo dinero en {self._history.benchmark_name}", "●"))
        for color, text, glyph in entries:
            dot = QLabel(glyph)
            dot.setStyleSheet(f"color: {color}; font-size: 12px; background: transparent;")
            self._legend.addWidget(dot)
            label = QLabel(text)
            label.setObjectName("chartLegendLabel")
            self._legend.addWidget(label)
            self._legend.addSpacing(12)
        self._legend.addStretch()

    def _has_benchmark(self) -> bool:
        return bool(self._history.benchmark_name) and any(
            p.benchmark_eur is not None for p in self._points)

    def _update_summary(self) -> None:
        points = self._points
        if len(points) < 2:
            self._lbl_summary.setText("")
            return
        first, last = points[0], points[-1]
        # Value minus FIFO cost = unrealized gain; its change is independent of
        # how much money was put in or taken out during the period.
        start_gain = first.value_eur - first.invested_eur
        end_gain = last.value_eur - last.invested_eur
        text = (f"Del {fmt_date(first.day)} al {fmt_date(last.day)} la plusvalía latente ha pasado "
                f"de {fmt_eur(start_gain, signed=True)} a {fmt_eur(end_gain, signed=True)} "
                f"({fmt_eur(end_gain - start_gain, signed=True)}).")
        if self._has_benchmark() and first.benchmark_eur is not None and last.benchmark_eur is not None:
            diff = last.value_eur - last.benchmark_eur
            text += (f" Frente a {self._history.benchmark_name}: "
                     f"{fmt_eur(diff, signed=True)} {'a tu favor' if diff >= 0 else 'en contra'} hoy.")
        self._lbl_summary.setText(text)

    # -- painting ------------------------------------------------------------
    def _paint(self, painter: QPainter, rect: QRectF) -> None:
        t = self._t
        points = self._points
        if len(points) < 2:
            painter.setPen(QColor(t["text_secondary"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Aún no hay historial suficiente para dibujar la evolución.")
            return
        pad_l, pad_r, pad_t, pad_b = 64.0, 14.0, 10.0, 26.0
        area = QRectF(rect.x() + pad_l, rect.y() + pad_t,
                      rect.width() - pad_l - pad_r, rect.height() - pad_t - pad_b)
        show_bench = self._has_benchmark()
        values = [p.value_eur for p in points] + [p.invested_eur for p in points]
        if show_bench:
            values += [p.benchmark_eur for p in points if p.benchmark_eur is not None]
        low, high = min(values), max(values)
        span = (high - low) or max(abs(high), 1.0)
        low, high = max(0.0, low - span * 0.08), high + span * 0.08
        d0, d1 = points[0].day.toordinal(), points[-1].day.toordinal()
        dspan = max(d1 - d0, 1)

        def x_of(day: date) -> float:
            return area.left() + area.width() * (day.toordinal() - d0) / dspan

        def y_of(value: float) -> float:
            return area.bottom() - area.height() * (value - low) / (high - low)

        # Grid + Y labels
        painter.setFont(QFont("Segoe UI", 8))
        for step in range(5):
            value = low + (high - low) * step / 4
            y = y_of(value)
            painter.setPen(QPen(QColor(t["border"]), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QColor(t["text_secondary"]))
            painter.drawText(QRectF(rect.x(), y - 9, pad_l - 8, 18),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             _short_eur(value))

        # X labels (about six, at month starts when the span allows)
        ticks = 6
        for i in range(ticks + 1):
            day = date.fromordinal(d0 + round(dspan * i / ticks))
            label = (f"{day.day} {_MONTHS[day.month - 1]}" if dspan <= 92
                     else f"{_MONTHS[day.month - 1]} {day.year % 100:02d}")
            x = x_of(day)
            # Keep the first/last labels inside the plot instead of clipping them.
            if i == 0:
                box, align = QRectF(x, area.bottom() + 4, 80, pad_b - 4), Qt.AlignmentFlag.AlignLeft
            elif i == ticks:
                box, align = QRectF(x - 80, area.bottom() + 4, 80, pad_b - 4), Qt.AlignmentFlag.AlignRight
            else:
                box, align = QRectF(x - 40, area.bottom() + 4, 80, pad_b - 4), Qt.AlignmentFlag.AlignHCenter
            painter.drawText(box, align | Qt.AlignmentFlag.AlignTop, label)

        def path_of(getter) -> QPainterPath:
            path = QPainterPath()
            started = False
            for p in points:
                v = getter(p)
                if v is None:
                    continue
                pt = QPointF(x_of(p.day), y_of(v))
                if started:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    started = True
            return path

        value_color = QColor(t["investment"])
        # Area under the portfolio value
        area_path = path_of(lambda p: p.value_eur)
        area_path.lineTo(QPointF(x_of(points[-1].day), area.bottom()))
        area_path.lineTo(QPointF(x_of(points[0].day), area.bottom()))
        area_path.closeSubpath()
        gradient = QLinearGradient(0, area.top(), 0, area.bottom())
        top_color = QColor(value_color)
        top_color.setAlpha(70)
        bottom_color = QColor(value_color)
        bottom_color.setAlpha(5)
        gradient.setColorAt(0, top_color)
        gradient.setColorAt(1, bottom_color)
        painter.fillPath(area_path, QBrush(gradient))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(t["text_secondary"]), 1.5, Qt.PenStyle.DashLine))
        painter.drawPath(path_of(lambda p: p.invested_eur))
        if show_bench:
            painter.setPen(QPen(QColor(BENCHMARK_COLOR), 1.6))
            painter.drawPath(path_of(lambda p: p.benchmark_eur))
        painter.setPen(QPen(value_color, 2.2))
        painter.drawPath(path_of(lambda p: p.value_eur))

        self._paint_hover(painter, area, points, x_of, y_of, show_bench)

    def _paint_hover(self, painter, area, points, x_of, y_of, show_bench) -> None:
        hx = self._canvas.hover_x
        if hx is None or not (area.left() <= hx <= area.right()):
            return
        t = self._t
        point = min(points, key=lambda p: abs(x_of(p.day) - hx))
        x = x_of(point.day)
        painter.setPen(QPen(QColor(t["border_strong"]), 1))
        painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        dots = [(point.value_eur, t["investment"]), (point.invested_eur, t["text_secondary"])]
        if show_bench and point.benchmark_eur is not None:
            dots.append((point.benchmark_eur, BENCHMARK_COLOR))
        for value, color in dots:
            painter.setPen(QPen(QColor(t["bg_secondary"]), 2))
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(x, y_of(value)), 4.5, 4.5)

        gain = point.value_eur - point.invested_eur
        lines = [fmt_date(point.day),
                 f"Cartera: {fmt_eur(point.value_eur)}",
                 f"Invertido: {fmt_eur(point.invested_eur)}",
                 f"Plusvalía: {fmt_eur(gain, signed=True)}"]
        if show_bench and point.benchmark_eur is not None:
            lines.append(f"Índice: {fmt_eur(point.benchmark_eur)}")
        painter.setFont(QFont("Segoe UI", 9))
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(line) for line in lines) + 20
        height = metrics.height() * len(lines) + 14
        bx = x + 12 if x + 12 + width < area.right() else x - 12 - width
        box = QRectF(bx, area.top() + 8, width, height)
        painter.setPen(QPen(QColor(t["border"]), 1))
        painter.setBrush(QColor(t["bg_secondary"]))
        painter.drawRoundedRect(box, 6, 6)
        for i, line in enumerate(lines):
            painter.setPen(QColor(t["text_primary"] if i else t["text_secondary"]))
            if i == 3:
                painter.setPen(QColor(t["income"] if gain >= 0 else t["expense"]))
            painter.drawText(QRectF(box.left() + 10, box.top() + 7 + i * metrics.height(),
                                    width - 20, metrics.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)

    # -- theme ---------------------------------------------------------------
    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        t = theme
        self._card.setStyleSheet(f"""
            QFrame#chartCard {{ background-color: {t["bg_secondary"]}; border-radius: 12px;
                               border: 1px solid {t["border"]}; }}
            QLabel#chartTitle {{ color: {t["text_primary"]}; font-size: 14px; font-weight: 700; }}
            QLabel#chartLegendLabel {{ color: {t["text_secondary"]}; font-size: 12px; }}
            QLabel#chartSummary {{ color: {t["text_secondary"]}; font-size: 12px; }}
            QPushButton#periodButton {{
                background: transparent; color: {t["text_secondary"]};
                border: 1px solid {t["border"]}; border-radius: 6px;
                padding: 0 10px; font-size: 12px; font-weight: 600;
            }}
            QPushButton#periodButton:checked {{
                background-color: {t["accent_light"]}; color: {t["accent"]};
                border-color: {t["accent"]};
            }}
            QPushButton#periodButton:hover {{ background-color: {t["bg_hover"]}; }}
        """)
        apply_card_shadow(self._card, t)
        self._rebuild_legend()
        self._canvas.update()


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
class AllocationPanel(QWidget):

    def __init__(self, session: Session, user_id: int, theme: dict) -> None:
        super().__init__()
        self._session = session
        self._user_id = user_id
        self._t = theme
        self._dimension = "security"
        self._snapshot = None
        self._data = []
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        bar = QHBoxLayout()
        self._group = QButtonGroup(self)
        for key, label in ALLOCATION_DIMENSIONS.items():
            button = QPushButton(label)
            button.setObjectName("periodButton")
            button.setCheckable(True)
            button.setChecked(key == self._dimension)
            button.setFixedHeight(30)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=key: self._set_dimension(k))
            self._group.addButton(button)
            bar.addWidget(button)
        bar.addStretch()
        root.addLayout(bar)

        self._chart = _CategoryChartWidget(theme)
        self._chart.setMinimumHeight(360)
        self._chart._canvas.setMinimumHeight(300)
        root.addWidget(self._chart)
        root.addStretch()

    @property
    def data(self):
        return self._data

    def reload(self, snapshot=None) -> None:
        self._snapshot = snapshot
        self._refresh()

    def _set_dimension(self, key: str) -> None:
        self._dimension = key
        self._refresh()

    def _refresh(self) -> None:
        self._data = allocation(self._session, self._user_id, self._dimension,
                                snapshot=self._snapshot)
        self._chart._lbl_title.setText(
            f"Distribución de la cartera · {ALLOCATION_DIMENSIONS[self._dimension].lower()}")
        self._chart.set_data(self._data)

    def apply_theme(self, theme: dict) -> None:
        self._t = theme
        t = theme
        self._chart.apply_theme(theme)
        self._chart.setStyleSheet(f"""
            QFrame#chartCard {{ background-color: {t["bg_secondary"]}; border-radius: 12px;
                               border: 1px solid {t["border"]}; }}
            QLabel#chartTitle {{ color: {t["text_primary"]}; font-size: 14px; font-weight: 700; }}
            QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
        """)
        self.setStyleSheet(f"""
            QPushButton#periodButton {{
                background: transparent; color: {t["text_secondary"]};
                border: 1px solid {t["border"]}; border-radius: 6px;
                padding: 0 12px; font-size: 12px; font-weight: 600;
            }}
            QPushButton#periodButton:checked {{
                background-color: {t["accent_light"]}; color: {t["accent"]};
                border-color: {t["accent"]};
            }}
        """)
