"""Database initialisation, integrity checks, recovery, and session factory."""

import gc
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base, User

logger = logging.getLogger(__name__)


def get_legacy_data_dir() -> Path:
    """Resolve the repo-local data folder used by development builds.

    In packaged builds the module is nested under a dist/_internal tree, so a
    simple parents[2] lookup points to a bundle-local folder instead of the real
    repository checkout. Search the local ancestry for a data/ directory that
    actually contains the PROFIN database so legacy user data can still be found.
    """
    module_path = Path(__file__).resolve()
    candidates: list[Path] = []

    for base in [module_path, module_path.parent, *module_path.parents]:
        candidates.extend(
            [
                base,
                base / "data",
                base / "PROFIN" / "data",
            ]
        )
    for base in [Path.cwd(), Path.cwd().parent, *Path.cwd().parents]:
        candidates.extend(
            [
                base,
                base / "data",
                base / "PROFIN" / "data",
            ]
        )
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent if sys.executable else None
        if exe_dir is not None:
            for base in [exe_dir, *exe_dir.parents]:
                candidates.extend([base, base / "data", base / "PROFIN" / "data"])

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        db_file = resolved / "profin.db" if resolved.is_dir() else None
        if db_file and db_file.exists():
            return resolved

    fallback = (module_path.parents[2] / "data").resolve()
    return fallback


def get_default_data_dir() -> Path:
    """Default writable location for the user profile on Windows."""
    if getattr(sys, "frozen", False):
        base_dir = os.environ.get("LOCALAPPDATA")
        root = Path(base_dir) if base_dir else Path.home() / "AppData" / "Local"
        return (root / "PROFIN" / "data").resolve()
    return get_legacy_data_dir()


def get_user_app_dir() -> Path:
    """Return the writable app directory for the current execution mode."""
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        base_dir = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return (base_dir / "PROFIN").resolve()
    return Path(__file__).resolve().parents[2].resolve()


def _database_has_users(db_path: Path) -> bool:
    """Return True when the SQLite database contains at least one user row."""
    if not db_path.exists():
        return False
    connection = None
    try:
        connection = sqlite3.connect(db_path)
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if not table_exists:
            return False
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return int(count or 0) > 0
    except sqlite3.DatabaseError:
        return False
    finally:
        if connection is not None:
            connection.close()


def _migrate_legacy_database_if_needed(default_data_dir: Path, legacy_data_dir: Path) -> bool:
    """Copy a populated legacy DB into the packaged app folder if the current target is empty or stale."""
    legacy_db = legacy_data_dir / "profin.db"
    target_db = default_data_dir / "profin.db"
    if not legacy_db.exists():
        return False
    if _database_has_users(target_db):
        return False

    if "engine" in globals():
        engine.dispose()
    if "SessionLocal" in globals():
        SessionLocal.close_all()
    gc.collect()

    default_data_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source = legacy_data_dir / f"profin.db{suffix}"
        target = default_data_dir / f"profin.db{suffix}"
        if source.exists():
            if target.exists():
                target.unlink(missing_ok=True)
            shutil.copy2(source, target)
    return True


def get_user_data_dir() -> Path:
    """Return the writable data directory, preferring the user-configured value when set."""
    settings = QSettings("GEVernova", "PROFIN")
    default_data_dir = get_default_data_dir()
    legacy_data_dir = get_legacy_data_dir()

    configured = settings.value("settings/data_folder", "", type=str)
    if configured:
        data_dir = Path(configured).expanduser().resolve()
        if getattr(sys, "frozen", False):
            if data_dir.resolve() == legacy_data_dir.resolve() or not _database_has_users(data_dir / "profin.db"):
                if _database_has_users(default_data_dir / "profin.db") or _database_has_users(legacy_data_dir / "profin.db"):
                    _migrate_legacy_database_if_needed(default_data_dir, legacy_data_dir)
                    settings.setValue("settings/data_folder", str(default_data_dir))
                    settings.sync()
                    return default_data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    if getattr(sys, "frozen", False) and legacy_data_dir.exists() and legacy_data_dir != default_data_dir:
        if _migrate_legacy_database_if_needed(default_data_dir, legacy_data_dir):
            settings.setValue("settings/data_folder", str(default_data_dir))
            settings.sync()
    default_data_dir.mkdir(parents=True, exist_ok=True)
    return default_data_dir


def refresh_runtime_paths() -> Path:
    """Refresh the live DB location after a user moves or configures the data directory."""
    global DB_DIR, DB_PATH, DATABASE_URL, engine
    DB_DIR = get_user_data_dir()
    DB_PATH = DB_DIR / "profin.db"
    DATABASE_URL = f"sqlite:///{DB_PATH}"
    if "engine" in globals():
        engine.dispose()
    engine = _build_engine(DATABASE_URL)
    SessionLocal.configure(bind=engine)
    return DB_DIR


DB_DIR = get_user_data_dir()
DB_PATH = DB_DIR / "profin.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"


def _set_sqlite_pragma(dbapi_conn, _connection_record) -> None:
    """Enable foreign keys and WAL mode for every new connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.close()


def _build_engine(database_url: str = DATABASE_URL):
    database_engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    event.listen(database_engine, "connect", _set_sqlite_pragma)
    return database_engine


engine = _build_engine()
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine, autocommit=False, autoflush=False
)


def get_session() -> Session:
    """Return a new SQLAlchemy session. Caller is responsible for closing it."""
    return SessionLocal()


# New installations start with no accounts. Users are created explicitly from
# the registration flow instead of receiving shared demo credentials.
DEFAULT_BOOTSTRAP_USERS: tuple[tuple[str, str], ...] = ()


def _sqlite_integrity_errors(path: Path) -> list[str]:
    """Return SQLite integrity errors, or a diagnostic if the file is unreadable."""
    try:
        with sqlite3.connect(path) as connection:
            return [
                row[0]
                for row in connection.execute("PRAGMA integrity_check")
                if row[0] != "ok"
            ]
    except sqlite3.DatabaseError as exc:
        return [str(exc)]


def _copy_recoverable_rows(
    source: sqlite3.Connection, target: sqlite3.Connection
) -> dict[str, int]:
    """Copy readable rows into a clean database, skipping invalid rows only."""
    table_order = (
        "users",
        "accounts",
        "categories",
        "import_batches",
        "transactions",
        "auto_categorization_rules",
        "audit_log",
    )
    copied: dict[str, int] = {}
    target.execute("PRAGMA foreign_keys = OFF")

    for table in table_order:
        try:
            source_columns = [
                row[1] for row in source.execute(f'PRAGMA table_info("{table}")')
            ]
            target_columns = [
                row[1] for row in target.execute(f'PRAGMA table_info("{table}")')
            ]
            columns = [column for column in source_columns if column in target_columns]
            if not columns:
                continue

            quoted = ", ".join(f'"{column}"' for column in columns)
            rows = source.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid')
            count = 0
            for row in rows:
                try:
                    target.execute(
                        f'INSERT OR IGNORE INTO "{table}" ({quoted}) '
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        row,
                    )
                    count += target.execute("SELECT changes()").fetchone()[0]
                except sqlite3.DatabaseError:
                    logger.warning("Fila no recuperable omitida: tabla=%s", table)
            copied[table] = count
        except sqlite3.DatabaseError as exc:
            logger.error("No se pudo recuperar la tabla %s: %s", table, exc)

    target.commit()
    target.execute("PRAGMA foreign_keys = ON")
    return copied


def _recover_corrupt_database(errors: list[str]) -> None:
    """Replace a corrupt SQLite file with a validated recoverable copy."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = DB_DIR / f"profin.corrupt.{timestamp}.db"
    recovery_path = DB_DIR / f"profin.recovery.{timestamp}.db"
    shutil.copy2(DB_PATH, backup_path)

    recovery_engine = _build_engine(f"sqlite:///{recovery_path}")
    Base.metadata.create_all(bind=recovery_engine)
    recovery_engine.dispose()

    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(recovery_path) as target:
        copied = _copy_recoverable_rows(source, target)
        target.execute("VACUUM")

    recovery_errors = _sqlite_integrity_errors(recovery_path)
    if recovery_errors:
        recovery_path.unlink(missing_ok=True)
        raise RuntimeError(
            "La reparación no superó integrity_check: "
            + "; ".join(recovery_errors[:3])
        )

    engine.dispose()
    DB_PATH.unlink()
    recovery_path.replace(DB_PATH)
    logger.warning(
        "Base SQLite reparada. Copia original=%s filas=%s diagnóstico=%s",
        backup_path,
        copied,
        errors[:3],
    )


def _ensure_database_integrity() -> None:
    errors = _sqlite_integrity_errors(DB_PATH)
    if errors:
        _recover_corrupt_database(errors)


def _refresh_database_bindings() -> None:
    global engine
    engine.dispose()
    engine = _build_engine()
    SessionLocal.configure(bind=engine)


def ensure_demo_users(session: Session) -> list[User]:
    """Keep the legacy bootstrap hook without creating automatic demo users."""
    if session.query(User).count() > 0:
        return []

    from app.auth.auth_service import create_user

    created: list[User] = []
    for username, password in DEFAULT_BOOTSTRAP_USERS:
        try:
            created.append(create_user(session, username, password))
        except ValueError:
            continue
    return created


def init_db() -> None:
    """Create tables, repair corruption, and seed first-run credentials."""
    refresh_runtime_paths()
    if DB_PATH.exists():
        _ensure_database_integrity()
    Base.metadata.create_all(bind=engine)
    _refresh_database_bindings()
    with SessionLocal() as session:
        ensure_demo_users(session)
