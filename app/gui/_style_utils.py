"""
app/gui/_style_utils.py
=========================
FAMILYFIN — Small shared GUI style helpers.

Currently provides card elevation (drop shadow) built from the theme's
``card_shadow`` token, so it can be applied consistently to every card-like
container across views and re-applied whenever the theme toggles.
"""
from __future__ import annotations

import re

from PySide6.QtGui     import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

_RGBA_RE = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)"
)


def _parse_shadow_color(card_shadow: str) -> QColor:
    """Parse a 'rgba(r,g,b,a)' token string into a QColor. Falls back to a
    sensible translucent black if the token is missing/malformed."""
    match = _RGBA_RE.match(card_shadow or "")
    if not match:
        return QColor(0, 0, 0, 60)
    r, g, b = (int(match.group(i)) for i in (1, 2, 3))
    alpha = float(match.group(4)) if match.group(4) is not None else 1.0
    color = QColor(r, g, b)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def apply_card_shadow(widget: QWidget, theme: dict,
                       blur_radius: int = 20,
                       x_offset: int = 0,
                       y_offset: int = 4) -> None:
    """
    Apply (or refresh) a soft drop shadow on ``widget`` using the theme's
    ``card_shadow`` token. Safe to call repeatedly (e.g. on every theme
    toggle) — it replaces any existing effect rather than stacking them.
    """
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur_radius)
    effect.setOffset(x_offset, y_offset)
    effect.setColor(_parse_shadow_color(theme.get("card_shadow", "")))
    widget.setGraphicsEffect(effect)
