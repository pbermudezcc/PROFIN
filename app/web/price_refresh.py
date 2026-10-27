"""
app/web/price_refresh.py
========================
FAMILYFIN — Price refresh requested from the web (W9b).

The phone or iPad never talks to Yahoo Finance: it asks the PC, which runs
the same refresh as the desktop's "Actualizar cotizaciones" button
(prices.refresh_market_data) in a background thread with its own database
session. Prices are shared by the whole family, so there is one job per
server: a request while it runs just shows its progress, and a new one is
not started within MIN_INTERVAL of the last (Yahoo is not hammered by
repeated taps). prices.REFRESH_LOCK keeps it from overlapping a refresh
started by the desktop app.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.investments.prices import RefreshResult, refresh_market_data

logger = logging.getLogger(__name__)

MIN_INTERVAL = 60.0          # seconds between two refreshes started from the web

STARTED, RUNNING, RECENT, CLOSED = "started", "running", "recent", "closed"


def market_provider(app):
    """The app's market-data provider (Yahoo Finance unless a test set one)."""
    provider = getattr(app.state, "market_provider", None)
    if provider is None:
        from app.investments.market_data import YahooProvider
        provider = app.state.market_provider = YahooProvider()
    return provider


def session_factory(app) -> Callable[[], Session]:
    """Sessions for background threads (requests get theirs from get_db)."""
    factory = getattr(app.state, "session_factory", None)
    if factory is not None:
        return factory
    from app.db import database
    return lambda: database.SessionLocal()       # read at call time, like get_db


def start_price_refresh(app, user_id: int) -> str:
    """Start the server's refresh on behalf of a user (STARTED / RUNNING / RECENT).
    When it succeeds the desktop app is told to reload that user's view."""
    on_change = getattr(app.state, "on_change", None)

    def done(result: RefreshResult) -> None:
        if on_change is not None and result.ok:
            on_change(user_id, "investments")

    return app.state.price_refresh.start(session_factory(app), market_provider(app), done)


class PriceRefreshJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="familyfin-web-prices")
        self._future: Optional[Future] = None
        self._finished_at: Optional[float] = None        # monotonic, for MIN_INTERVAL
        self.last_result: Optional[RefreshResult] = None

    @property
    def running(self) -> bool:
        return self._future is not None and not self._future.done()

    def start(self, make_session: Callable[[], Session], provider,
              on_done: Optional[Callable[[RefreshResult], None]] = None) -> str:
        """STARTED, or why not: RUNNING (wait for it) / RECENT (just refreshed) /
        CLOSED (the server is stopping)."""
        with self._lock:
            if self.running:
                return RUNNING
            if (self._finished_at is not None and self.last_result is not None and self.last_result.ok
                    and time.monotonic() - self._finished_at < MIN_INTERVAL):
                return RECENT
            try:
                self._future = self._executor.submit(self._run, make_session, provider, on_done)
            except RuntimeError:          # the server is stopping: never fail the caller
                logger.info("Web: servidor deteniéndose, no se actualizan cotizaciones")
                return CLOSED
            return STARTED

    def _run(self, make_session, provider, on_done) -> RefreshResult:
        session = make_session()
        try:
            result = refresh_market_data(session, provider)
        except Exception as exc:          # a DB problem must not vanish in the thread
            logger.exception("Web: error inesperado al actualizar cotizaciones")
            result = RefreshResult(started_at=datetime.now(), finished_at=datetime.now(),
                                   error=f"No se pudieron guardar las cotizaciones: {exc}")
        finally:
            session.close()
        with self._lock:
            self.last_result, self._finished_at = result, time.monotonic()
        if on_done is not None:
            try:
                on_done(result)
            except Exception:
                logger.exception("Web: aviso tras actualizar cotizaciones")
        return result

    def wait(self, timeout: Optional[float] = None) -> Optional[RefreshResult]:
        """Block until the running refresh ends (tests, shutdown)."""
        future = self._future
        return future.result(timeout=timeout) if future is not None else None

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
