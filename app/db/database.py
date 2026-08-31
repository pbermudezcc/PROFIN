"""Database initialisation, integrity checks, recovery, and session factory."""

from datetime import datetime
from pathlib import Path
import logging
import shutil
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from app.db.models import Base, User

logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).resolve().parents[2] / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
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
    if DB_PATH.exists():
        _ensure_database_integrity()
    Base.metadata.create_all(bind=engine)
    _refresh_database_bindings()
    with SessionLocal() as session:
        ensure_demo_users(session)
