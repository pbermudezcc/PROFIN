"""Draw the FAMILYFIN web/PWA icons (app/web/static/icons/*.png) with Qt.

Run once after changing the design:  python tools/make_web_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

OUT = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "icons"


def draw(size: int, rounded: bool) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor("#1E40AF"))
    gradient.setColorAt(0.55, QColor("#2563EB"))
    gradient.setColorAt(1.0, QColor("#7C3AED"))
    p.setBrush(QBrush(gradient))
    p.setPen(Qt.PenStyle.NoPen)
    if rounded:
        p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.225, size * 0.225)
    else:  # iOS / maskable icons are full-bleed; the OS rounds the corners
        p.drawRect(QRectF(0, 0, size, size))

    s = size / 100.0
    # Rising bars
    p.setBrush(QColor(255, 255, 255, 70))
    for i, h in enumerate((22, 34, 48)):
        x = 24 + i * 15
        p.drawRoundedRect(QRectF(x * s, (74 - h) * s, 10 * s, h * s), 2.5 * s, 2.5 * s)
    # Trend line
    path = QPainterPath(QPointF(20 * s, 64 * s))
    path.lineTo(QPointF(40 * s, 50 * s))
    path.lineTo(QPointF(54 * s, 57 * s))
    path.lineTo(QPointF(78 * s, 32 * s))
    pen = QPen(QColor("white"), 7 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("white"))
    p.drawEllipse(QPointF(78 * s, 32 * s), 6 * s, 6 * s)
    p.end()
    return image


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    OUT.mkdir(parents=True, exist_ok=True)
    draw(180, rounded=False).save(str(OUT / "apple-touch-icon.png"))
    draw(192, rounded=True).save(str(OUT / "icon-192.png"))
    draw(512, rounded=False).save(str(OUT / "icon-512.png"))
    print("Iconos escritos en", OUT)


if __name__ == "__main__":
    main()
