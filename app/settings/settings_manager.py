from __future__ import annotations

import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QSettings

from app.db.database import (
    PENDING_RESTORE_NAME,
    database_has_users,
    get_default_data_dir,
    get_user_data_dir,
    snapshot_database,
)

logger = logging.getLogger(__name__)


@dataclass
class AppSettings:
    theme: str = "light"
    save_appearance_on_start: bool = True
    language: str = "es"
    default_currency: str = "EUR"
    default_period: str = "Mes actual"
    auto_backup: bool = True
    backup_interval_hours: int = 24
    backup_folder: str = ""
    weekly_summary_email: bool = False
    enable_hints: bool = True
    require_password_on_start: bool = True
    protect_sensitive_data: bool = True
    import_folder: str = ""
    export_folder: str = ""
    data_folder: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SettingsManager:
    """Simple, stable persistence layer for application configuration."""

    ORG = "FAMILYFIN"
    APP = "FAMILYFIN"

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings or QSettings(self.ORG, self.APP)
        self._defaults = AppSettings()

    def load(self) -> AppSettings:
        values = {
            "theme": self._settings.value("settings/theme", self._defaults.theme, type=str),
            "save_appearance_on_start": self._settings.value(
                "settings/save_appearance_on_start",
                self._defaults.save_appearance_on_start,
                type=bool,
            ),
            "language": self._settings.value("settings/language", self._defaults.language, type=str),
            "default_currency": self._settings.value(
                "settings/default_currency", self._defaults.default_currency, type=str
            ),
            "default_period": self._settings.value(
                "settings/default_period", self._defaults.default_period, type=str
            ),
            "auto_backup": self._settings.value(
                "settings/auto_backup", self._defaults.auto_backup, type=bool
            ),
            "backup_interval_hours": self._settings.value(
                "settings/backup_interval_hours", self._defaults.backup_interval_hours, type=int
            ),
            "backup_folder": self._settings.value(
                "settings/backup_folder", self._defaults.backup_folder, type=str
            ),
            "weekly_summary_email": self._settings.value(
                "settings/weekly_summary_email", self._defaults.weekly_summary_email, type=bool
            ),
            "enable_hints": self._settings.value(
                "settings/enable_hints", self._defaults.enable_hints, type=bool
            ),
            "require_password_on_start": self._settings.value(
                "settings/require_password_on_start", self._defaults.require_password_on_start, type=bool
            ),
            "protect_sensitive_data": self._settings.value(
                "settings/protect_sensitive_data", self._defaults.protect_sensitive_data, type=bool
            ),
            "import_folder": self._settings.value(
                "settings/import_folder", self._defaults.import_folder, type=str
            ),
            "export_folder": self._settings.value(
                "settings/export_folder", self._defaults.export_folder, type=str
            ),
            "data_folder": self._settings.value(
                "settings/data_folder", str(get_user_data_dir()), type=str
            ),
        }
        return AppSettings(**values)

    def save(self, settings: AppSettings) -> None:
        self._settings.setValue("settings/theme", settings.theme)
        self._settings.setValue("settings/save_appearance_on_start", settings.save_appearance_on_start)
        self._settings.setValue("settings/language", settings.language)
        self._settings.setValue("settings/default_currency", settings.default_currency)
        self._settings.setValue("settings/default_period", settings.default_period)
        self._settings.setValue("settings/auto_backup", settings.auto_backup)
        self._settings.setValue("settings/backup_interval_hours", settings.backup_interval_hours)
        self._settings.setValue("settings/backup_folder", settings.backup_folder)
        self._settings.setValue("settings/weekly_summary_email", settings.weekly_summary_email)
        self._settings.setValue("settings/enable_hints", settings.enable_hints)
        self._settings.setValue("settings/require_password_on_start", settings.require_password_on_start)
        self._settings.setValue("settings/protect_sensitive_data", settings.protect_sensitive_data)
        self._settings.setValue("settings/import_folder", settings.import_folder)
        self._settings.setValue("settings/export_folder", settings.export_folder)
        self._settings.setValue("settings/data_folder", settings.data_folder or str(get_default_data_dir()))
        self._settings.sync()

    def ensure_dirs(self, settings: AppSettings) -> None:
        for folder_key in ("import_folder", "export_folder", "backup_folder", "data_folder"):
            folder = getattr(settings, folder_key)
            if folder:
                Path(folder).mkdir(parents=True, exist_ok=True)

    def apply_data_folder(self, folder: str, current_dir: Path | None = None) -> Path:
        """Point the app at a new data directory, copying the current database there.

        A target folder that already holds a database with users is used as
        it is and never overwritten (it may be another copy of the family's
        data). A user-less database there is kept aside as
        profin.replaced.<timestamp>.db before the current one is copied in.
        The copy is a consistent SQLite snapshot (includes pending WAL).
        current_dir is the folder in use now; pass it when the setting may
        already have been overwritten (e.g. after save()).
        The caller must re-initialise the database (init_db) before using it.
        """
        target_dir = Path(folder).expanduser().resolve() if folder else get_default_data_dir()
        target_dir.mkdir(parents=True, exist_ok=True)

        current_dir = current_dir or get_user_data_dir()
        if current_dir.resolve() != target_dir.resolve():
            source_db = current_dir / "profin.db"
            target_db = target_dir / "profin.db"
            if database_has_users(target_db):
                logger.info(
                    "La carpeta %s ya contiene una base con usuarios; se usa sin sobrescribirla.",
                    target_dir,
                )
            elif source_db.exists():
                if target_db.exists():
                    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    target_db.replace(target_dir / f"profin.replaced.{timestamp}.db")
                    for suffix in ("-wal", "-shm"):
                        (target_dir / f"profin.db{suffix}").unlink(missing_ok=True)
                snapshot_database(source_db, target_db)
                logger.info("Base de datos copiada a %s", target_db)

        self._settings.setValue("settings/data_folder", str(target_dir))
        self._settings.sync()
        return target_dir

    def backup_all_data(
        self,
        backup_dir: str,
        user_name: str | None = None,
        prefix: str = "profin_backup",
    ) -> Path:
        if not backup_dir:
            raise ValueError("Debe seleccionar una carpeta de respaldo.")

        backup_root = Path(backup_dir)
        backup_root.mkdir(parents=True, exist_ok=True)

        db_path = get_user_data_dir() / "profin.db"

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_user = (user_name or "usuario").replace(" ", "_").lower()
        backup_path = backup_root / f"{prefix}_{safe_user}_{timestamp}.zip"

        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "app": "FAMILYFIN",
            "user": safe_user,
            "database_files": ["profin.db"],
            "settings_keys": sorted(self._settings.allKeys()),
        }

        with tempfile.TemporaryDirectory() as tmp:
            # Consistent single-file snapshot (includes pending WAL content),
            # instead of zipping profin.db/-wal/-shm while they may change.
            snapshot_path = Path(tmp) / "profin.db"
            snapshot_database(db_path, snapshot_path)

            with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("metadata.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                snapshot = {key: self._settings.value(key) for key in self._settings.allKeys()}
                archive.writestr("settings.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
                archive.write(snapshot_path, arcname="data/profin.db")

        return backup_path

    # -- Automatic backups ---------------------------------------------

    AUTO_BACKUP_PREFIX = "profin_autobackup"
    AUTO_BACKUP_KEEP = 10
    KEY_LAST_AUTO_BACKUP = "backup/last_auto_backup"

    def auto_backup_dir(self, settings: AppSettings) -> Path:
        """Configured backup folder, or a backups/ folder next to the data."""
        if settings.backup_folder:
            return Path(settings.backup_folder).expanduser()
        return get_user_data_dir().parent / "backups"

    def run_auto_backup_if_due(
        self,
        user_name: str | None = None,
        now: datetime | None = None,
    ) -> Path | None:
        """Create an automatic backup when enabled and the interval has passed.

        Returns the new backup path, or None when no backup was due. Only the
        oldest *automatic* backups are pruned; manual ones are never deleted.
        """
        settings = self.load()
        if not settings.auto_backup:
            return None

        now = now or datetime.now()
        last_raw = self._settings.value(self.KEY_LAST_AUTO_BACKUP, "", type=str)
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
            except ValueError:
                last = None
            if last is not None and now - last < timedelta(hours=settings.backup_interval_hours):
                return None

        backup_dir = self.auto_backup_dir(settings)
        backup_path = self.backup_all_data(
            str(backup_dir), user_name=user_name, prefix=self.AUTO_BACKUP_PREFIX
        )
        self._settings.setValue(self.KEY_LAST_AUTO_BACKUP, now.isoformat(timespec="seconds"))
        self._settings.sync()

        # Each backup holds the whole family database, whoever was logged
        # in, so keep the newest N overall (by time, not by name).
        old_backups = sorted(
            backup_dir.glob(f"{self.AUTO_BACKUP_PREFIX}_*.zip"),
            key=lambda p: p.stat().st_mtime,
        )
        for stale in old_backups[: -self.AUTO_BACKUP_KEEP]:
            stale.unlink(missing_ok=True)

        logger.info("Copia de seguridad automática creada: %s", backup_path)
        return backup_path

    def restore_all_data(self, backup_path: str) -> bool:
        """Validate a backup and stage it; it is applied on the next app start.

        The live profin.db is never overwritten while the app has it open.
        Settings are restored immediately, except the data folder, which
        stays pointing at this machine's current location.
        """
        archive_path = Path(backup_path)
        if not archive_path.exists():
            return False

        db_dir = get_user_data_dir()

        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if "metadata.json" not in names or "data/profin.db" not in names:
                return False

            pending = db_dir / PENDING_RESTORE_NAME
            with archive.open("data/profin.db") as src, open(pending, "wb") as dst:
                shutil.copyfileobj(src, dst)

            if "settings.json" in names:
                settings_payload = json.loads(archive.read("settings.json").decode("utf-8"))
                for key in list(self._settings.allKeys()):
                    self._settings.remove(key)
                for key, value in settings_payload.items():
                    self._settings.setValue(key, value)
                self._settings.setValue("settings/data_folder", str(db_dir))
                self._settings.sync()

        return True
