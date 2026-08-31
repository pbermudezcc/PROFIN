"""
Database initialisation and session factory.
Uses SQLAlchemy 2.x with SQLite backend.
Alembic is used for schema migrations (configured separately).
"""

from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from app.db.models import Base, User

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


DEFAULT_BOOTSTRAP_USERS = (
    ("usuario", "123456"),
    ("admin", "123456"),
)


def ensure_demo_users(session: Session) -> list[User]:
    """Seed a default user for first login if the database is empty."""
    if session.query(User).count() > 0:
        return []

    created: list[User] = []
    for username, password in DEFAULT_BOOTSTRAP_USERS:
        try:
            from app.auth.auth_service import create_user
            created.append(create_user(session, username, password))
        except ValueError:
            continue
    return created


def init_db() -> None:
    """Create all tables if they do not exist and seed an initial user if needed."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        ensure_demo_users(session)