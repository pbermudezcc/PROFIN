"""
app/investments/refresh_worker.py
=================================
FAMILYFIN — Price refresh off the GUI thread.

A SQLAlchemy Session is not thread-safe, so the worker opens its own
short-lived session (same pattern as the import worker) and only hands a
RefreshResult back through a signal. PriceRefresher owns the thread, never
runs two refreshes at once, and remembers when the last one finished so
the view can apply the 15-minute staleness rule.

Usage from a view:
    refresher = PriceRefresher(parent=self)
    refresher.finished.connect(self._on_prices_refreshed)
    refresher.refresh_if_stale()      # on showEvent
    refresher.refresh()               # "Actualizar" button
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal
from sqlalchemy.orm import Session

from app.investments.market_data import MarketDataProvider, YahooProvider
from app.investments.prices import RefreshResult, is_stale, refresh_market_data

logger = logging.getLogger(__name__)


def _default_session() -> Session:
    from app.db.database import get_session
    return get_session()


class PriceRefreshWorker(QObject):
    finished = Signal(object)   # RefreshResult

    def __init__(
        self,
        provider: MarketDataProvider,
        session_factory: Callable[[], Session] = _default_session,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._session_factory = session_factory

    def run(self) -> None:
        session = self._session_factory()
        try:
            result = refresh_market_data(session, self._provider)
        except Exception as exc:        # a DB problem must not kill the thread silently
            logger.exception("Error inesperado al actualizar cotizaciones")
            result = RefreshResult(
                started_at=datetime.now(), finished_at=datetime.now(),
                error=f"No se pudieron guardar las cotizaciones: {exc}",
            )
        finally:
            session.close()
        self.finished.emit(result)


class PriceRefresher(QObject):
    """One refresh at a time, plus the time of the last successful one."""

    started = Signal()
    finished = Signal(object)   # RefreshResult

    def __init__(
        self,
        provider: Optional[MarketDataProvider] = None,
        session_factory: Callable[[], Session] = _default_session,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider or YahooProvider()
        self._session_factory = session_factory
        self._thread: Optional[QThread] = None
        self._worker: Optional[PriceRefreshWorker] = None
        self.last_success: Optional[datetime] = None
        self.last_result: Optional[RefreshResult] = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def refresh_if_stale(self) -> bool:
        if is_stale(self.last_success):
            return self.refresh()
        return False

    def refresh(self) -> bool:
        """Start a refresh; False if one is already running."""
        if self.running:
            return False
        self._thread = QThread()
        self._worker = PriceRefreshWorker(self._provider, self._session_factory)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_worker_finished)
        self._thread.start()
        self.started.emit()
        return True

    def _on_worker_finished(self, result: RefreshResult) -> None:
        thread, self._thread, self._worker = self._thread, None, None
        if thread is not None:
            thread.quit()
            thread.wait()
            thread.deleteLater()
        self.last_result = result
        if result.ok:
            self.last_success = result.finished_at
        self.finished.emit(result)

    def shutdown(self) -> None:
        """Wait for a running refresh (call before the app closes)."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
