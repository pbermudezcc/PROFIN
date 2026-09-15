"""
FAMILYFIN — Personal & Project Finance Tracker
Entry point. Initialises DB, runs auth flow, launches main window.
"""

import logging
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from app.db.database import get_user_app_dir, init_db
from app.gui.login_window import LoginWindow


def _setup_logging() -> None:
    """
    Configure application-wide logging:
      - Console handler : INFO level,  compact format  → VSCode terminal
      - File handler    : DEBUG level, full format     → logs/profin.log
      - Rotating        : 1 MB max per file, 3 backups kept
      - Third-party     : sqlalchemy / PIL silenced at WARNING
    """
    from logging.handlers import RotatingFileHandler

    app_root = get_user_app_dir()
    log_dir = app_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "profin.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        fmt     = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt = "%H:%M:%S",
    ))

    file_handler = RotatingFileHandler(
        str(log_file),
        maxBytes    = 1_048_576,
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

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging iniciado — consola: INFO | fichero DEBUG: %s", log_file
    )


def _show_fatal_error(message: str, detailed: str | None = None) -> None:
    """Mostrar un QMessageBox con el error y registrar la traza."""
    logging.getLogger(__name__).exception(message)
    # If QApplication isn't created yet, try to create a temporary one to show the dialog.
    try:
        if QApplication.instance() is None:
            _tmp = QApplication([])
            QMessageBox.critical(None, "Error crítico - FAMILYFIN", message + ("\n\n" + detailed if detailed else ""))
            _tmp.quit()
        else:
            QMessageBox.critical(None, "Error crítico - FAMILYFIN", message + ("\n\n" + detailed if detailed else ""))
    except Exception:
        # Fallback to console output if QMessageBox fails for any reason
        print(message)
        if detailed:
            print(detailed)


def _unhandled_exception(exc_type, exc, tb) -> None:
    """Hook global para excepciones no capturadas: registra y muestra al usuario."""
    formatted = "".join(traceback.format_exception(exc_type, exc, tb))
    logging.getLogger(__name__).error("Unhandled exception:\n%s", formatted)
    # Mostrar un cuadro crítico con información mínima
    try:
        QMessageBox.critical(None, "Error inesperado", "Se ha producido un error inesperado. Por favor revisa los logs (logs/profin.log) y reinicia la aplicación.")
    except Exception:
        # Si no hay GUI disponible, imprimir al stdout
        print("Unhandled exception:")
        print(formatted)


def main() -> None:
    _setup_logging()
    # Registrar hook global de excepciones
    sys.excepthook = _unhandled_exception

    logging.getLogger(__name__).info(
        "FAMILYFIN arrancando — Python %s", sys.version.split()[0]
    )

    # ── HiDPI: mejor visual en pantallas de alta densidad ─────────────
    QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # ── Inicializar DB de forma segura y notificar si falla ──────────
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - queremos capturar cualquier fallo aquí para informar al usuario
        tb = traceback.format_exc()
        _show_fatal_error("No se pudo inicializar la base de datos.", tb)
        return

    # ── Crear la aplicación Qt y lanzar la ventana de login ───────────
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("FAMILYFIN")
    # Fusion es fiable; el tema y paleta pueden personalizarse en app/gui/theme.py
    qt_app.setStyle("Fusion")

    window = LoginWindow()
    window.show()

    # Guardar la excepción global también en el bucle de eventos (por si Qt intercepta algo)
    return_code = qt_app.exec()
    logging.getLogger(__name__).info("FAMILYFIN terminado con código %s", return_code)
    sys.exit(return_code)

if __name__ == "__main__":
    main()