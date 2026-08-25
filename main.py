"""
PROFIN — Personal & Project Finance Tracker
Entry point. Initialises DB, runs auth flow, launches main window.
"""

import logging
import sys
from PySide6.QtWidgets import QApplication
from app.db.database import init_db
from app.gui.login_window import LoginWindow


def _setup_logging() -> None:
    """
    Configure application-wide logging:
      - Console handler : INFO level,  compact format  → VSCode terminal
      - File handler    : DEBUG level, full format     → logs/profin.log
      - Rotating        : 1 MB max per file, 3 backups kept
      - Third-party     : sqlalchemy / PIL silenced at WARNING
    """
    import os
    from logging.handlers import RotatingFileHandler

    # ── Create logs/ directory next to main.py ────────────────────────
    log_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "profin.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)          # capture everything at root level

    # ── Remove any handlers added by basicConfig or Qt ────────────────
    root.handlers.clear()

    # ── Console handler — INFO+ ───────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        fmt     = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt = "%H:%M:%S",
    ))

    # ── File handler — DEBUG+, rotating 1 MB × 3 files ───────────────
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes    = 1_048_576,   # 1 MB
        backupCount = 3,
        encoding    = "utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt     = "%(asctime)s  %(levelname)-8s  %(name)-40s  %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    ))

    root.addHandler(console)
    root.addHandler(file_handler)

    # ── Silence noisy third-party loggers ─────────────────────────────
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    # ── Startup confirmation ──────────────────────────────────────────
    logging.getLogger(__name__).info(
        "Logging iniciado — consola: INFO | fichero DEBUG: %s", log_file
    )


def main() -> None:
    _setup_logging()
    logging.getLogger(__name__).info(
        "PROFIN arrancando — Python %s", sys.version.split()[0]
    )
    init_db()
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("PROFIN")
    qt_app.setStyle("Fusion")
    window = LoginWindow()
    window.show()
    sys.exit(qt_app.exec())

if __name__ == "__main__":
    main()