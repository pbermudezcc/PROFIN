"""Database initialisation, integrity checks, recovery, and session factory."""

import gc
import logging
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, close_all_sessions, sessionmaker

from app.db.models import Base, User

logger = logging.getLogger(__name__)


def get_legacy_data_dir() -> Path:
    """Resolve the historical repository data folder for one-time migration.

    In packaged builds the module is nested under a dist/_internal tree, so a
    simple parents[2] lookup points to a bundle-local folder instead of the real
    repository checkout. Search the local ancestry for a data/ directory that
    actually contains the PROFIN database so legacy user data can still be found.
    """
    module_path = Path(__file__).resolve()
    candidates: list[Path] = []

    for base in [module_path.parent, *module_path.parents]:
        candidates.extend(
            [
                base / "data",
                base / "PROFIN" / "data",
            ]
        )
    for base in [Path.cwd(), Path.cwd().parent, *Path.cwd().parents]:
        candidates.extend(
            [
                base / "data",
                base / "PROFIN" / "data",
            ]
        )
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent if sys.executable else None
        if exe_dir is not None:
            for base in [exe_dir, *exe_dir.parents]:
                candidates.extend([base / "data", base / "PROFIN" / "data"])

    # Lowest priority: the old packaged install's LOCALAPPDATA folder, for
    # dev-mode runs that wouldn't otherwise traverse into it.
    legacy_local_app_data = os.environ.get("LOCALAPPDATA")
    if legacy_local_app_data:
        candidates.append(Path(legacy_local_app_data) / "PROFIN" / "data")

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
    """Return the shared writable location used by development and frozen builds."""
    base_dir = os.environ.get("LOCALAPPDATA")
    root = Path(base_dir) if base_dir else Path.home() / "AppData" / "Local"
    return (root / "FAMILYFIN" / "data").resolve()


def get_user_app_dir() -> Path:
    """Return the writable app directory for the current execution mode."""
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        base_dir = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return (base_dir / "FAMILYFIN").resolve()
    return Path(__file__).resolve().parents[2].resolve()


def database_has_users(db_path: Path) -> bool:
    """Return True when the SQLite database contains at least one user row."""
    if not db_path.exists():
        return False
    try:
        with closing(sqlite3.connect(db_path)) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            if not table_exists:
                return False
            count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return int(count or 0) > 0
    except sqlite3.DatabaseError:
        return False


def _migrate_legacy_database_if_needed(default_data_dir: Path, legacy_data_dir: Path) -> bool:
    """Copy a populated legacy DB into the packaged app folder if the current target is empty or stale."""
    legacy_db = legacy_data_dir / "profin.db"
    target_db = default_data_dir / "profin.db"
    if not legacy_db.exists():
        return False
    if database_has_users(target_db):
        return False

    if "engine" in globals():
        engine.dispose()
    if "SessionLocal" in globals():
        close_all_sessions()
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


# Older builds stored preferences under a previous organization name; read-only, for migration.
_LEGACY_SETTINGS_ORG = "GEVernova"
LEGACY_QSETTINGS_STORES = ((_LEGACY_SETTINGS_ORG, "FAMILYFIN"), (_LEGACY_SETTINGS_ORG, "PROFIN"))


def _migrate_legacy_qsettings_if_needed(settings: QSettings) -> None:
    """Copy saved preferences from the newest non-empty legacy QSettings store, once, on first run under the new name."""
    if settings.allKeys():
        return
    for org, app in LEGACY_QSETTINGS_STORES:
        legacy_settings = QSettings(org, app)
        legacy_keys = legacy_settings.allKeys()
        if not legacy_keys:
            continue
        for key in legacy_keys:
            settings.setValue(key, legacy_settings.value(key))
        settings.sync()
        return


def get_user_data_dir() -> Path:
    """Return the writable data directory, preferring the user-configured value when set."""
    settings = QSettings("FAMILYFIN", "FAMILYFIN")
    _migrate_legacy_qsettings_if_needed(settings)
    default_data_dir = get_default_data_dir()
    legacy_data_dir = get_legacy_data_dir()

    configured = settings.value("settings/data_folder", "", type=str)
    if configured:
        data_dir = Path(configured).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Directorio de datos configurado: %s (frozen=%s)",
            data_dir,
            getattr(sys, "frozen", False),
        )
        return data_dir

    if legacy_data_dir.exists() and legacy_data_dir != default_data_dir:
        if _migrate_legacy_database_if_needed(default_data_dir, legacy_data_dir):
            settings.setValue("settings/data_folder", str(default_data_dir))
            settings.sync()
    default_data_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Directorio de datos seleccionado: %s (frozen=%s)",
        default_data_dir,
        getattr(sys, "frozen", False),
    )
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


def _build_engine(database_url: str | None = None):
    # The default is resolved at call time: a `= DATABASE_URL` default would
    # be frozen at import and silently rebind the app to the old database
    # after the data folder changes (refresh_runtime_paths / init_db).
    if database_url is None:
        database_url = DATABASE_URL
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


def get_database_users(data_dir: Path) -> list[str]:
    """Return usernames from a readable database, sorted case-insensitively."""
    db_path = data_dir / "profin.db"
    if not db_path.is_file():
        return []

    with closing(sqlite3.connect(db_path)) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone():
            return []
        return [
            row[0]
            for row in connection.execute(
                "SELECT username FROM users ORDER BY username COLLATE NOCASE"
            )
            if row[0]
        ]


# New installations start with no accounts. Users are created explicitly from
# the registration flow instead of receiving shared demo credentials.
DEFAULT_BOOTSTRAP_USERS: tuple[tuple[str, str], ...] = ()


def _sqlite_integrity_errors(path: Path) -> list[str]:
    """Return SQLite integrity errors, or a diagnostic if the file is unreadable."""
    try:
        with closing(sqlite3.connect(path)) as connection:
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


def snapshot_database(source: Path, target: Path) -> None:
    """Write a consistent copy of a (possibly open, WAL-mode) SQLite file.

    Uses SQLite's online backup API, so pending WAL content is included and
    the copy is never a half-written file, unlike copying profin.db by hand.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------
# create_all() only creates missing tables; it never adds columns to an
# existing database. Each migration below upgrades an existing file by one
# version, tracked in SQLite's built-in PRAGMA user_version. Steps must be
# idempotent: a step that fails half-way is simply re-run on the next start.

def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _migration_1_category_source(db_path: Path) -> None:
    """Add transactions.category_source and backfill it for existing rows.

    Existing categorized rows are marked 'rule' when the current rules would
    assign exactly that category, and 'manual' otherwise, so a later
    "re-apply all rules" can no longer overwrite a family member's fixes.
    """
    from app.categorization.rules_engine import apply_rules
    from app.db.models import CATEGORY_SOURCE_MANUAL, CATEGORY_SOURCE_RULE, Transaction

    with closing(sqlite3.connect(db_path)) as connection:
        if not _column_exists(connection, "transactions", "category_source"):
            connection.execute(
                "ALTER TABLE transactions ADD COLUMN category_source VARCHAR(16)"
            )
            connection.commit()

    migration_engine = _build_engine(f"sqlite:///{db_path}")
    try:
        with Session(migration_engine) as session:
            pending = (
                session.query(Transaction)
                .filter(
                    Transaction.category_id.isnot(None),
                    Transaction.category_source.is_(None),
                )
                .all()
            )
            by_user: dict[int, list[Transaction]] = {}
            for txn in pending:
                by_user.setdefault(txn.user_id, []).append(txn)

            counts = {CATEGORY_SOURCE_RULE: 0, CATEGORY_SOURCE_MANUAL: 0}
            for user_id, txns in by_user.items():
                result = apply_rules(session, [t.id for t in txns], user_id)
                for txn in txns:
                    source = (
                        CATEGORY_SOURCE_RULE
                        if result.assigned.get(txn.id) == txn.category_id
                        else CATEGORY_SOURCE_MANUAL
                    )
                    txn.category_source = source
                    counts[source] += 1
            session.commit()
            logger.info("Migración 1: category_source rellenado %s", counts)
    finally:
        migration_engine.dispose()


def _migration_2_indexes_and_cents(db_path: Path) -> None:
    """Add the transaction indexes and round stored money to whole cents.

    Index names match Transaction.__table_args__, so new databases created by
    create_all() and upgraded ones end up identical. Amounts parsed from the
    bank export can carry float noise (e.g. 12.339999999); rounding makes the
    duplicate-detection keys and SQL sums exact to the cent. Both steps are
    idempotent.
    """
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS ix_transactions_user_date
                ON transactions (user_id, transaction_date);
            CREATE INDEX IF NOT EXISTS ix_transactions_user_category
                ON transactions (user_id, category_id);
            CREATE INDEX IF NOT EXISTS ix_transactions_user_account_date
                ON transactions (user_id, account_id, transaction_date);
            UPDATE transactions
               SET amount = ROUND(amount, 2)
             WHERE amount != ROUND(amount, 2);
            UPDATE transactions
               SET running_balance = ROUND(running_balance, 2)
             WHERE running_balance IS NOT NULL
               AND running_balance != ROUND(running_balance, 2);
            """
        )
        connection.commit()
    logger.info("Migración 2: índices de transacciones creados e importes redondeados a céntimos")


def _migration_3_fix_cross_user_categories(db_path: Path) -> None:
    """Repair transactions that point at another user's category.

    Before global rules were resolved per user, a global rule assigned its
    creator's category id to every user's transactions. Each such row is
    moved to the equivalent category in its own user's tree (same name,
    level and parent type name); without an equivalent the category is
    cleared so the rules engine can fill it again. Idempotent.
    """
    with closing(sqlite3.connect(db_path)) as connection:
        wrong = connection.execute(
            """
            SELECT t.id, t.user_id, c.name, c.level, p.name
              FROM transactions t
              JOIN categories c ON c.id = t.category_id
              LEFT JOIN categories p ON p.id = c.parent_id
             WHERE c.user_id != t.user_id
            """
        ).fetchall()
        remapped = cleared = 0
        for txn_id, user_id, name, level, parent_name in wrong:
            own = connection.execute(
                """
                SELECT c.id
                  FROM categories c
                  LEFT JOIN categories p ON p.id = c.parent_id
                 WHERE c.user_id = ? AND c.name = ? AND c.level = ?
                   AND p.name IS ?
                 ORDER BY c.id
                 LIMIT 1
                """,
                (user_id, name, level, parent_name),
            ).fetchone()
            if own:
                connection.execute(
                    "UPDATE transactions SET category_id = ? WHERE id = ?",
                    (own[0], txn_id),
                )
                remapped += 1
            else:
                connection.execute(
                    "UPDATE transactions SET category_id = NULL, category_source = NULL "
                    "WHERE id = ?",
                    (txn_id,),
                )
                cleared += 1
        connection.commit()
    logger.info(
        "Migración 3: categorías de otro usuario corregidas (reasignadas=%d, vaciadas=%d)",
        remapped, cleared,
    )


def _migration_4_investments(db_path: Path) -> None:
    """Create the investments tables (securities, ledger, price and FX cache).

    Purely additive: no existing row is touched. init_db() already runs
    create_all() before migrating, but the step creates the tables itself so
    migrate_schema() alone upgrades a file too. create_all(checkfirst) also
    creates the ledger indexes declared in the model; IF NOT EXISTS covers a
    table that exists without them. Idempotent.
    """
    from app.db.models import FxRate, InvestmentTransaction, PriceHistory, Security

    migration_engine = _build_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(
            migration_engine,
            tables=[
                Security.__table__,
                InvestmentTransaction.__table__,
                PriceHistory.__table__,
                FxRate.__table__,
            ],
        )
    finally:
        migration_engine.dispose()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS ix_investment_transactions_user_date
                ON investment_transactions (user_id, trade_date);
            CREATE INDEX IF NOT EXISTS ix_investment_transactions_user_security
                ON investment_transactions (user_id, security_id);
            """
        )
        connection.commit()
    logger.info("Migración 4: tablas de inversiones creadas")


def _migration_5_broker_ref(db_path: Path) -> None:
    """Add investment_transactions.broker_ref and its partial unique index.

    Imported broker statements store the broker's id of each operation so a
    re-import skips what is already there. Existing (hand-typed) rows keep
    NULL, which the partial index ignores. Idempotent.
    """
    with closing(sqlite3.connect(db_path)) as connection:
        if not _column_exists(connection, "investment_transactions", "broker_ref"):
            connection.execute(
                "ALTER TABLE investment_transactions ADD COLUMN broker_ref VARCHAR(64)"
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_investment_transactions_broker_ref
                ON investment_transactions (user_id, broker, broker_ref)
                WHERE broker_ref IS NOT NULL
            """
        )
        connection.commit()
    logger.info("Migración 5: referencia del bróker en las operaciones de inversión")


SCHEMA_MIGRATIONS = (
    (1, _migration_1_category_source),
    (2, _migration_2_indexes_and_cents),
    (3, _migration_3_fix_cross_user_categories),
    (4, _migration_4_investments),
    (5, _migration_5_broker_ref),
)
SCHEMA_VERSION = SCHEMA_MIGRATIONS[-1][0]


def migrate_schema(db_path: Path, backup_first: bool = True) -> int:
    """Bring db_path up to SCHEMA_VERSION. Returns the version it started at.

    When backup_first is set and any step is pending, a snapshot is written
    next to the database (profin.pre-migration-vN.<timestamp>.db) first.
    """
    with closing(sqlite3.connect(db_path)) as connection:
        start_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if start_version >= SCHEMA_VERSION:
        return start_version

    if backup_first:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = db_path.with_name(
            f"profin.pre-migration-v{SCHEMA_VERSION}.{timestamp}.db"
        )
        snapshot_database(db_path, backup_path)
        logger.info("Copia previa a la migración: %s", backup_path)

    for version, step in SCHEMA_MIGRATIONS:
        if version <= start_version:
            continue
        logger.info("Aplicando migración de esquema v%d (%s)", version, step.__name__)
        step(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(f"PRAGMA user_version = {int(version)}")
            connection.commit()
    return start_version


def init_db() -> None:
    """Create tables, repair corruption, migrate the schema, seed first-run users."""
    refresh_runtime_paths()
    apply_pending_restore(DB_DIR)
    existed_before = DB_PATH.exists()
    if existed_before:
        _ensure_database_integrity()
    Base.metadata.create_all(bind=engine)
    migrate_schema(DB_PATH, backup_first=existed_before)
    _refresh_database_bindings()
    with SessionLocal() as session:
        ensure_demo_users(session)


# ---------------------------------------------------------------------------
# Deferred restore
# ---------------------------------------------------------------------------
# Restoring a backup while the app has profin.db open (WAL mode) can corrupt
# it. A restore is therefore staged as profin.restore-pending.db and swapped
# in at the next start, before any connection is opened.

PENDING_RESTORE_NAME = "profin.restore-pending.db"


def apply_pending_restore(data_dir: Path) -> bool:
    """Swap a staged restore into place. Returns True if one was applied."""
    pending = data_dir / PENDING_RESTORE_NAME
    if not pending.exists():
        return False

    errors = _sqlite_integrity_errors(pending)
    if errors:
        logger.error("Restauración descartada, el fichero no es válido: %s", errors[:3])
        pending.unlink()
        return False

    engine.dispose()
    db_path = data_dir / "profin.db"
    if db_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        previous = data_dir / f"profin.before-restore.{timestamp}.db"
        snapshot_database(db_path, previous)
        logger.info("Copia de la base anterior a la restauración: %s", previous)
    for suffix in ("-wal", "-shm"):
        (data_dir / f"profin.db{suffix}").unlink(missing_ok=True)
    pending.replace(db_path)
    logger.warning("Restauración aplicada desde copia de seguridad.")
    return True
