"""
Database initialisation and session factory.
Uses SQLAlchemy 2.x with SQLite backend.
Alembic is used for schema migrations (configured separately).
"""

from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from app.db.models import Base

# ---------------------------------------------------------------------------
# Path configuration
# ---------------------------------------------------------------------------
DB_DIR = Path(__file__).resolve().parents[2] / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "profin.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

# ---------------------------------------------------------------------------
# Engine — enforce foreign keys on every connection (SQLite default is OFF)
# ---------------------------------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,  # Set True for SQL debug logging
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record) -> None:
    """Enable FK enforcement and WAL mode for every new SQLite connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.close()


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


def get_session() -> Session:
    """Return a new SQLAlchemy session. Caller is responsible for closing it."""
    return SessionLocal()


def init_db() -> None:
    """Create all tables if they do not exist (first-run bootstrap)."""
    Base.metadata.create_all(bind=engine)