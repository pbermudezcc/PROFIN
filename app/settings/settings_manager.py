from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings

from app.db.database import get_default_data_dir, get_user_data_dir, refresh_runtime_paths


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

    ORG = "GEVernova"
    APP = "PROFIN"

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
                "settings/data_folder", str(get_default_data_dir()), type=str
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

    def apply_data_folder(self, folder: str) -> Path:
        """Copy the current user database to a new data directory and persist the new path for the next app launch."""
        target_dir = Path(folder).expanduser().resolve() if folder else get_default_data_dir()
        target_dir.mkdir(parents=True, exist_ok=True)

        current_dir = get_user_data_dir()
        if current_dir.resolve() == target_dir.resolve():
            self._settings.setValue("settings/data_folder", str(target_dir))
            self._settings.sync()
            return target_dir

        source_db = current_dir / "profin.db"
        target_db = target_dir / "profin.db"
        if source_db.exists() and (
            not target_db.exists() or target_db.stat().st_size == 0
        ):
            for suffix in ("", "-wal", "-shm"):
                source_file = current_dir / f"profin.db{suffix}"
                target_file = target_dir / f"profin.db{suffix}"
                if source_file.exists():
                    if target_file.exists():
                        target_file.unlink()
                    shutil.copy2(source_file, target_file)
        else:
            for suffix in ("", "-wal", "-shm"):
                source_file = current_dir / f"profin.db{suffix}"
                if source_file.exists():
                    shutil.copy2(source_file, target_dir / source_file.name)

        self._settings.setValue("settings/data_folder", str(target_dir))
        self._settings.sync()
        return target_dir

    def backup_all_data(self, backup_dir: str, user_name: str | None = None) -> Path:
        if not backup_dir:
            raise ValueError("Debe seleccionar una carpeta de respaldo.")

        backup_root = Path(backup_dir)
        backup_root.mkdir(parents=True, exist_ok=True)

        db_dir = get_user_data_dir()
        db_path = db_dir / "profin.db"
        db_files = [db_path]
        for suffix in ("-wal", "-shm"):
            candidate = db_path.with_name(db_path.name + suffix)
            if candidate.exists():
                db_files.append(candidate)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_user = (user_name or "usuario").replace(" ", "_").lower()
        backup_path = backup_root / f"profin_backup_{safe_user}_{timestamp}.zip"

        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "app": "PROFIN",
            "user": safe_user,
            "database_files": [p.name for p in db_files],
            "settings_keys": sorted(self._settings.allKeys()),
        }

        with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("metadata.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            snapshot = {key: self._settings.value(key) for key in self._settings.allKeys()}
            archive.writestr("settings.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
            for file_path in db_files:
                if file_path.exists():
                    archive.write(file_path, arcname=f"data/{file_path.name}")

        return backup_path

    def restore_all_data(self, backup_path: str) -> bool:
        archive_path = Path(backup_path)
        if not archive_path.exists():
            return False

        db_dir = get_user_data_dir()
        db_path = db_dir / "profin.db"

        with zipfile.ZipFile(archive_path, "r") as archive:
            if "metadata.json" not in archive.namelist():
                return False
            manifest = json.loads(archive.read("metadata.json"))
            db_names = manifest.get("database_files", ["profin.db"])

            settings_payload = json.loads(archive.read("settings.json").decode("utf-8"))
            for key in list(self._settings.allKeys()):
                self._settings.remove(key)
            for key, value in settings_payload.items():
                self._settings.setValue(key, value)
            self._settings.sync()

            for name in db_names:
                if name == "profin.db":
                    archive_name = "data/profin.db"
                    if archive_name in archive.namelist():
                        with archive.open(archive_name) as src, open(db_path, "wb") as dst:
                            dst.write(src.read())
                    break

            if "data/profin.db" in archive.namelist() and not db_path.exists():
                with archive.open("data/profin.db") as src, open(db_path, "wb") as dst:
                    dst.write(src.read())

        return True
