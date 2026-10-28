"""
app/web/uploads.py
==================
FAMILYFIN — Statement uploads waiting for confirmation.

Between "upload + preview" and "confirm", the parsed statement stays on the
server (never in the browser): a random token maps to the preview, the user
and the web session that uploaded it. Entries expire after TTL seconds and
their temporary file is deleted with them.
"""
from __future__ import annotations

import logging
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TTL = 15 * 60
MAX_PER_USER = 3
PREFIX = "familyfin-import-"


@dataclass
class PendingImport:
    token: str
    user_id: int
    sid: str
    account_id: int
    filenames: List[str]
    directory: Path
    preview: Any                       # app.ingestion.importer.ImportPreview
    created: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > TTL

    @property
    def label(self) -> str:
        return self.filenames[0] if len(self.filenames) == 1 else f"{len(self.filenames)} extractos"


class PendingImports:
    def __init__(self) -> None:
        self._items: Dict[str, PendingImport] = {}
        self._lock = threading.Lock()

    @staticmethod
    def new_directory() -> Path:
        """Private temp folder for one upload (user-only permissions); the
        statements of a multi-file upload each get a numbered subfolder, so
        identically named exports never overwrite each other."""
        return Path(tempfile.mkdtemp(prefix=PREFIX))

    def add(self, user_id: int, sid: str, account_id: int, filenames: List[str],
            directory: Path, preview: Any) -> str:
        token = secrets.token_urlsafe(18)
        item = PendingImport(token, user_id, sid, account_id, list(filenames), directory, preview)
        with self._lock:
            self._sweep_locked()
            # Keep only the newest few per user.
            mine = sorted((p for p in self._items.values() if p.user_id == user_id), key=lambda p: p.created)
            for old in mine[: max(0, len(mine) - MAX_PER_USER + 1)]:
                self._drop_locked(old.token)
            self._items[token] = item
        return token

    def get(self, token: str, user_id: int, sid: str) -> Optional[PendingImport]:
        """Only the same user in the same browser session can see it."""
        with self._lock:
            self._sweep_locked()
            item = self._items.get(token)
            if item is None or item.user_id != user_id or item.sid != sid:
                return None
            return item

    def discard(self, token: str) -> None:
        with self._lock:
            self._drop_locked(token)

    def clear(self) -> None:
        with self._lock:
            for token in list(self._items):
                self._drop_locked(token)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    # -- internals ---------------------------------------------------------
    def _sweep_locked(self) -> None:
        for token in [t for t, item in self._items.items() if item.expired]:
            self._drop_locked(token)

    def _drop_locked(self, token: str) -> None:
        item = self._items.pop(token, None)
        if item is not None:
            remove_directory(item.directory)


def sweep_orphans(max_age: float = TTL) -> int:
    """Delete upload folders left behind by a crash or a killed process
    (older than any live preview can be). Returns how many were removed."""
    removed = 0
    now = time.time()
    for directory in Path(tempfile.gettempdir()).glob(PREFIX + "*"):
        try:
            if directory.is_dir() and now - directory.stat().st_mtime > max_age:
                remove_directory(directory)
                removed += 1
        except OSError:
            continue
    return removed


def remove_directory(directory: Path) -> None:
    try:
        shutil.rmtree(directory, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Web: no se pudo borrar la carpeta temporal %s", directory)
