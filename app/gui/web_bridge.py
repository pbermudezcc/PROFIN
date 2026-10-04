"""
app/gui/web_bridge.py
=====================
FAMILYFIN — Hands "data changed from the web" notices from the web server
thread to the Qt main thread.

The bridge object lives in the main thread, so emitting its signal from the
server thread is delivered as a queued call: slots always run in the GUI
thread, never concurrently with it.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal


class WebBridge(QObject):
    data_changed = Signal(int, str)   # user_id, area ("transactions" | "investments")


_bridge: Optional[WebBridge] = None


def web_bridge() -> WebBridge:
    """Create on first use — call it first from the main thread (main.py)."""
    global _bridge
    if _bridge is None:
        _bridge = WebBridge()
    return _bridge


def install() -> WebBridge:
    from app.web.server import get_web_server

    bridge = web_bridge()
    get_web_server().on_change = bridge.data_changed.emit
    return bridge
