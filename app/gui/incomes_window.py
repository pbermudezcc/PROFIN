from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
import qtawesome as qta

from app.config import THEMES


class IncomesWindow(QWidget):
    """Temporary screen for income navigation until the full income dashboard is implemented."""

    def __init__(self, user, session, theme: str = "light") -> None:
        super().__init__()
        self._user = user
        self._session = session
        self._theme = theme if theme in THEMES else "light"
        self._t = THEMES[self._theme]
        self._build_ui()
        self.apply_theme(self._theme)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)

        lbl_icon = QLabel()
        lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_icon.setPixmap(
            qta.icon("mdi.trending-up", color=self._t["income"]).pixmap(64, 64)
        )
        layout.addWidget(lbl_icon)

        lbl = QLabel("<b>Ingresos</b>")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"font-size: 20px; color: {self._t['text_secondary']};"
        )
        layout.addWidget(lbl)

        lbl2 = QLabel("En construcción — Próximas fases")
        lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl2.setStyleSheet(
            f"font-size: 13px; color: {self._t['neutral']};"
        )
        layout.addWidget(lbl2)

    def apply_theme(self, theme: str) -> None:
        self._theme = theme if theme in THEMES else "light"
        self._t = THEMES[self._theme]
        self.setStyleSheet(
            f"QWidget {{ background-color: {self._t['bg_primary']}; color: {self._t['text_primary']}; }}"
        )

    def refresh_theme(self, theme: str) -> None:
        self.apply_theme(theme)
