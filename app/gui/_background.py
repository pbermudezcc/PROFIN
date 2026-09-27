"""
app/gui/_background.py
======================
FAMILYFIN — Run a slow call (network) off the GUI thread.

    runner = BackgroundRunner(parent=self)
    runner.run(lambda: provider.search(text), self._on_results)

The callback runs back on the GUI thread as callback(result, error), with
exactly one of the two set. The callable must not use the GUI's shared
SQLAlchemy Session (it is not thread-safe): do the network part in the
thread and touch the database in the callback.

`latest_only=True` drops the result of a call that a newer latest_only
call has superseded (e.g. a search the user has already retyped).

Lifetime: each call runs in its own QThread subclass whose run() ends when
the call returns (no event loop to quit). Threads are kept alive in a
module-level registry until they finish, even if the dialog that started
them is closed — destroying a running QThread aborts the process. Closing
a runner only suppresses its callbacks. wait_for_background_threads() is
called when the main window closes.

Thread note: the `done` signal is emitted from the worker thread and is
connected to a bound method of the runner (a QObject living in the GUI
thread), so Qt queues the call there. A plain function or lambda would
run in the worker thread instead.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Set

from PySide6.QtCore import QObject, QThread, Signal, Slot

logger = logging.getLogger(__name__)

_LIVE: Set[QThread] = set()


def _prune() -> None:
    for thread in [t for t in _LIVE if t.isFinished()]:
        _LIVE.discard(thread)


def wait_for_background_threads(timeout_ms: int = 15000) -> None:
    """Block until every background call has returned (app shutdown)."""
    for thread in list(_LIVE):
        if not thread.wait(timeout_ms):
            logger.warning("Un hilo en segundo plano no terminó a tiempo")
    _prune()


class _Task(QThread):
    done = Signal(int, object, object)   # number, result, error

    def __init__(self, number: int, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._number = number
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:        # handed to the callback, never lost
            self.done.emit(self._number, None, exc)
        else:
            self.done.emit(self._number, result, None)


class BackgroundRunner(QObject):
    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._callbacks: Dict[int, tuple] = {}
        self._sequence = 0
        self._latest = 0
        self._closed = False

    @property
    def busy(self) -> bool:
        return bool(self._callbacks)

    def run(
        self,
        fn: Callable[[], Any],
        callback: Callable[[Any, Optional[BaseException]], None],
        latest_only: bool = False,
    ) -> int:
        _prune()
        self._sequence += 1
        number = self._sequence
        if latest_only:
            self._latest = number
        task = _Task(number, fn)
        _LIVE.add(task)
        self._callbacks[number] = (callback, latest_only)
        task.done.connect(self._on_done)
        task.start()
        return number

    @Slot(int, object, object)
    def _on_done(self, number: int, result, error) -> None:
        entry = self._callbacks.pop(number, None)
        if entry is None or self._closed:
            return
        callback, latest_only = entry
        if latest_only and number != self._latest:
            return
        if error is not None:
            logger.info("Tarea en segundo plano con error: %s", error)
        callback(result, error)

    def shutdown(self) -> None:
        """Stop delivering callbacks; running calls finish on their own."""
        self._closed = True
        self._callbacks.clear()
