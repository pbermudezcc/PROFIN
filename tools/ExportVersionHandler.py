"""
tools/ExportVersionHandler.py
==============================
PROFIN — Personal & Project Finance Tracker
Version Snapshot Handler
Handler version : 1.0.0
Author          : GE Vernova Power Systems Engineering / Pedro
Purpose
-------
EXPORT  : Collects all .py files + key assets -> single versioned .md file
          (always .md for Amp compatibility).
          Also creates a .prj portable archive (zip of all source
          files + manifest.json) required for restore/undo.
IMPORT  : Reads a .md or .prj snapshot, shows metadata, file
          list, live-tree diff. Optionally extracts .prj to a
          staging folder for safe review.
RESTORE : Applies a .prj snapshot back to the live project tree
          with per-file .bak backups and a full restore manifest.
UNDO    : Re-applies the most recent backup set, reverting the
          last restore operation.

Snapshot naming convention
--------------------------
PROFIN.B.X.X.md    -- Beta snapshot    (for Amp)
PROFIN.R.X.X.md    -- Release snapshot (for Amp)
PROFIN.B.X.X.prj   -- Beta portable archive
PROFIN.R.X.X.prj   -- Release portable archive

Changelog
---------
v1.0.0  Adapted from BIO.DCS.TOOL ExportVersionHandler v1.7.3.
        Project renamed PROFIN throughout.
        SCAN_DIRS updated for PROFIN src-layout structure.
        PRIORITY_FILES updated for PROFIN modules.
        Config path updated to app/config.py.
        Entry point changed from app.py to main.py.
        Asset discovery updated: alembic.ini + alembic/env.py included.
        Snapshot naming convention updated: PROFIN.B.X.X / PROFIN.R.X.X.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ==============================================================================
# CONSTANTS
# ==============================================================================

HANDLER_VERSION = "1.0.0"

# -- Project root (one level above tools/) ------------------------------------
ROOT = Path(__file__).resolve().parent.parent

# -- Output folders -----------------------------------------------------------
VERSIONS_DIR = ROOT / "versions"
RESTORE_DIR  = VERSIONS_DIR / "restores"

# ==============================================================================
# SCAN DIRECTORIES
# Auto-scan: all .py files found recursively under each listed folder.
# ==============================================================================
SCAN_DIRS = [
    "app",        # All subpackages: db, ingestion, auth, categorization,
                  #                   gui, visualization, export, utils
    "tests",      # Unit and integration tests
    "alembic",    # DB migration scripts
    "tools",      # This tool and any other dev tools
]

# ==============================================================================
# PRIORITY FILES
# These are always included first, in this exact order, regardless of
# auto-discovery. Ensures the most important files appear at the top of the
# snapshot for easy reading in Amp.
# ==============================================================================
PRIORITY_FILES = [
    # -- Root entry point & config -------------------------------------------
    "main.py",
    "app/config.py",

    # -- Database layer -------------------------------------------------------
    "app/db/__init__.py",
    "app/db/models.py",
    "app/db/database.py",

    # -- Authentication -------------------------------------------------------
    "app/auth/__init__.py",
    "app/auth/auth_service.py",
    "app/auth/session_manager.py",

    # -- Excel ingestion ------------------------------------------------------
    "app/ingestion/__init__.py",
    "app/ingestion/excel_parser.py",
    "app/ingestion/validator.py",
    "app/ingestion/duplicate_detector.py",
    "app/ingestion/importer.py",

    # -- Categorization -------------------------------------------------------
    "app/categorization/__init__.py",
    "app/categorization/category_manager.py",
    "app/categorization/rules_engine.py",

    # -- GUI ------------------------------------------------------------------
    "app/gui/__init__.py",
    "app/gui/main_window.py",
    "app/gui/dashboard.py",
    "app/gui/expenses_view.py",
    "app/gui/incomes_window.py",
    "app/gui/categories_view.py",
    "app/gui/import_view.py",
    "app/gui/login_window.py",
    "app/gui/rules_view.py",
    "app/gui/settings_view.py",
    "app/gui/transactions_view.py",

    # -- Settings --------------------------------------------------------------
    "app/settings/__init__.py",
    "app/settings/settings_manager.py",

    # -- Tests ----------------------------------------------------------------
    "tests/__init__.py",
    "tests/test_auth.py",
    "tests/test_duplicate_detector.py",
    "tests/test_validator.py",
    "tests/test_rules_engine.py",
    "tests/test_theme_views.py",

    # -- Alembic migration config ---------------------------------------------
    "tools/ExportVersionHandler.py",
]

# -- Extra non-.py assets included in snapshots --------------------------------
_ASSET_FILES = [
    "alembic.ini",        # Alembic DB migration config (root level)
    "CHANGELOG.md",       # Version history (root level)
    "requirements.txt",   # Dependency list (root level, if present)
]

# ==============================================================================
# AUTO-READ APP_NAME / APP_VERSION FROM app/config.py
# ==============================================================================

def _read_config_value(key: str, fallback: str) -> str:
    """
    Read a string constant from app/config.py using regex.
    Handles both single and double quote assignments.
    Returns `fallback` if the key is not found or file is unreadable.
    """
    config_path = ROOT / "app" / "config.py"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            r'^\s*' + re.escape(key) + r"""\s*=\s*["']([^"']+)["']""",
            text,
            re.MULTILINE,
        )
        if m:
            return m.group(1).strip()
    except (OSError, ValueError):
        pass
    return fallback


CURRENT_APP_NAME    = _read_config_value("APP_NAME",    "PROFIN")
CURRENT_APP_VERSION = _read_config_value("APP_VERSION", "0.0")


# ==============================================================================
# UTILITY HELPERS
# ==============================================================================

def _sep(char: str = "─", width: int = 62) -> str:
    return char * width


def _configure_console_encoding() -> None:
    """Use UTF-8 for console output, including legacy Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _ensure_dir(path: Path) -> None:
    """Create directory (and parents) if it does not exist."""
    try:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            print("  Created folder -> {}".format(path))
    except OSError as exc:
        print("  ERROR: Could not create directory {}: {}".format(path, exc))
        sys.exit(1)


def _check_existing_file(output_path: Path) -> bool:
    """
    Warn if target file already exists.
    Returns True -> proceed, False -> user cancelled.
    """
    if not output_path.exists():
        return True
    print("\n  WARNING: File already exists: {}".format(output_path.name))
    while True:
        choice = input("\n  Overwrite existing file? [y/n]: ").strip().lower()
        if choice == "y":
            print("  Proceeding with overwrite...")
            return True
        elif choice == "n":
            print("  Export cancelled by user.")
            return False
        else:
            print("  Please enter Y or N.")


def _snapshot_filename(app_name: str, ver_type: str, version: str) -> str:
    """
    Build the base snapshot filename (without extension).
    Format: PROFIN.B.1.0  or  PROFIN.R.1.0
    """
    return "{}.{}.{}".format(app_name, ver_type, version)


def _list_existing_versions() -> list:
    """
    Print a summary table of all .md and .prj snapshot files in VERSIONS_DIR.
    Returns the sorted list of found paths.
    """
    if not VERSIONS_DIR.exists():
        return []
    existing = sorted(
        list(VERSIONS_DIR.glob("{}.B.*.md".format(CURRENT_APP_NAME)))  +
        list(VERSIONS_DIR.glob("{}.R.*.md".format(CURRENT_APP_NAME)))  +
        list(VERSIONS_DIR.glob("{}.B.*.prj".format(CURRENT_APP_NAME))) +
        list(VERSIONS_DIR.glob("{}.R.*.prj".format(CURRENT_APP_NAME)))
    )
    if not existing:
        print("  (no previous exports found in versions/)")
        return []
    print("\n  {}".format(_sep("─", 58)))
    print("  Existing snapshots in versions/")
    print("  {}".format(_sep("─", 58)))
    for f in existing:
        size_kb = f.stat().st_size / 1024
        mtime   = datetime.fromtimestamp(
            f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if f.suffix == ".prj":
            ext_tag = "  .prj "
        elif ".B." in f.name:
            ext_tag = "B .md "
        else:
            ext_tag = "R .md "
        print("  {}  {:<46}  {:6.1f} KB   {}".format(
            ext_tag, f.name, size_kb, mtime))
    print("  {}".format(_sep("─", 58)))
    return existing


# ==============================================================================
# FILE DISCOVERY
# ==============================================================================

def _discover_py_files() -> List[str]:
    """
    Recursively scan SCAN_DIRS for all .py files.
    main.py at root is always added first if it exists.
    Returns a list of relative posix path strings.
    """
    discovered = []
    if (ROOT / "main.py").exists():
        discovered.append("main.py")
    for scan_dir in SCAN_DIRS:
        base = ROOT / scan_dir
        if not base.exists():
            continue
        for py_file in sorted(base.rglob("*.py")):
            rel = py_file.relative_to(ROOT).as_posix()
            if rel not in discovered:
                discovered.append(rel)
    return discovered


def _discover_assets() -> List[str]:
    """
    Include fixed non-.py asset files defined in _ASSET_FILES.
    Returns a list of relative posix path strings (only files that exist).
    """
    discovered = []
    for asset_rel in _ASSET_FILES:
        if (ROOT / asset_rel).exists():
            if asset_rel not in discovered:
                discovered.append(asset_rel)
    return discovered


def _build_file_list() -> Tuple[List[str], List[str]]:
    """
    Merge PRIORITY_FILES with auto-discovered files.
    Returns
    -------
    Tuple (ordered, auto_added)
    ordered   : full deterministic list (priority + extras)
    auto_added: only the files appended from auto-discovery
    """
    discovered   = _discover_py_files() + _discover_assets()
    priority_set = set(PRIORITY_FILES)
    extras       = [f for f in discovered if f not in priority_set]
    ordered      = PRIORITY_FILES + extras
    return ordered, extras


# ==============================================================================
# FENCE ESCAPING (BUG-EH-01 FIX)
# Prevents premature closure of markdown code fences when source
# code itself contains triple-backtick sequences.
# ==============================================================================

def _escape_fence(code: str, lang: str = "python") -> str:
    """
    Wrap `code` in a markdown fenced block.
    If `code` contains backtick sequences of length >= 3, use a longer fence
    to prevent premature closure (the root cause of Amp upload rejection).
    BUG-EH-05: never emit empty language tag.
    """
    max_run = 0
    run     = 0
    for ch in code:
        if ch == "`":
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    fence_len = max(3, max_run + 1)
    fence     = "`" * fence_len
    lang_tag  = lang if lang else "text"
    return "{}{}\n{}\n{}".format(fence, lang_tag, code, fence)


# ==============================================================================
# PROMPTS
# ==============================================================================

def _prompt_version_type() -> str:
    """
    Ask the user whether this is a Beta or Release snapshot.
    Returns "B" (Beta) or "R" (Release).
    """
    print("\n  {}".format(_sep("─", 58)))
    print("  VERSION TYPE")
    print("  {}".format(_sep("─", 58)))
    print("  [B]  Beta    -- work-in-progress  (PROFIN.B.X.X)")
    print("  [R]  Release -- production-ready  (PROFIN.R.X.X)")
    print("  {}".format(_sep("─", 58)))
    while True:
        choice = input("\n  Select version type [B/R]: ").strip().upper()
        if choice == "B":
            print("  Beta snapshot selected.")
            return "B"
        elif choice == "R":
            print("  Release snapshot selected.")
            print("  WARNING: Release snapshots should only be created")
            print("           for fully tested, production-ready code.")
            confirm = input("\n  Confirm Release? [y/n]: ").strip().lower()
            if confirm == "y":
                return "R"
            else:
                print("  Back to type selection...")
        else:
            print("  Please enter B or R.")


def _prompt_export_version(ver_type: str) -> Tuple[str, bool]:
    """
    Interactive CLI prompt for the version number.
    Returns
    -------
    Tuple (snapshot_version_label, is_override).
    is_override=True  -> option [1] re-export same version.
                         No description prompt. No CHANGELOG update.
    is_override=False -> option [2] new revision number.
                         Triggers description prompt + CHANGELOG update.
    """
    type_label = "Beta" if ver_type == "B" else "Release"
    print("\n  {}".format(_sep("=")))
    print("  PROFIN -- {} Export".format(type_label))
    print(_sep("="))
    print("  App name    (app/config.py) : {}".format(CURRENT_APP_NAME))
    print("  App version (app/config.py) : {}".format(CURRENT_APP_VERSION))
    print("  Handler version             : {}".format(HANDLER_VERSION))
    _list_existing_versions()
    print()
    print("  [1]  Override current revision  (re-export same version)")
    print("  [2]  Enter a new revision number")
    print(_sep("="))
    while True:
        choice = input("\n  Select an option [1/2]: ").strip()
        if choice == "1":
            print("\n  Overriding {}.{} ...".format(
                ver_type, CURRENT_APP_VERSION))
            return CURRENT_APP_VERSION, True
        elif choice == "2":
            while True:
                new_ver = input(
                    "\n  Enter new revision number "
                    "(e.g. 1.0, 1.1, 1.1a, 2.0): "
                ).strip()
                if not new_ver:
                    print("  Version cannot be empty. Try again.")
                    continue
                if not new_ver[0].isdigit():
                    print("  Version must start with a digit. Try again.")
                    continue
                snap_name = "{}.{}.{}".format(
                    CURRENT_APP_NAME, ver_type, new_ver)
                confirm = input(
                    "\n  Confirm -> {}.md  [y/n]: ".format(
                        snap_name)).strip().lower()
                if confirm == "y":
                    return new_ver, False
                else:
                    print("  Re-entering version...")
        else:
            print("  Invalid choice. Please enter 1 or 2.")


def _prompt_also_prj() -> bool:
    """
    Ask whether to also create a .prj portable archive.
    Default answer is YES (Enter alone accepts).
    Returns True or False.
    """
    print("\n  +------------------------------------------------------+")
    print("  |  .prj archive  (RECOMMENDED -- always create)       |")
    print("  |                                                      |")
    print("  |  .md   -> for Amp  (human-readable, always written) |")
    print("  |  .prj  -> for restore / undo  (portable archive)    |")
    print("  |                                                      |")
    print("  |  Without a .prj you CANNOT restore this version.    |")
    print("  +------------------------------------------------------+")
    while True:
        choice = input(
            "\n  Create .prj archive? [Y/n] (Enter = Y): "
        ).strip().lower()
        if choice in ("", "y"):
            print("  .prj archive will be created.")
            return True
        elif choice == "n":
            print("  WARNING: Skipping .prj -- restore will NOT be available.")
            return False
        else:
            print("  Please enter y, n, or press Enter for default (y).")


def _prompt_revision_description() -> str:
    """
    Prompt the user for a short description of this revision.
    Called ONLY for new revision exports (is_override=False).
    Empty input is accepted (stored as empty string).
    """
    print("\n  {}".format(_sep("─", 58)))
    print("  REVISION DESCRIPTION  (optional, press Enter to skip)")
    print("  {}".format(_sep("─", 58)))
    description = input("  Description: ").strip()
    if description:
        print("  Description saved: {}".format(description))
    else:
        print("  No description provided.")
    return description


# ==============================================================================
# CHANGELOG UPDATE
# ==============================================================================

def _update_changelog(
    ver_type:   str,
    version:    str,
    now:        str,
    file_count: int,
    description: str,
    snap_base:  str,
) -> None:
    """
    Create or update CHANGELOG.md in the project root directory.
    Called ONLY for new revision exports (is_override=False).
    Override re-exports do NOT touch CHANGELOG.md.
    If CHANGELOG.md does not exist -> bootstraps it with a branded header.
    New entries are PREPENDED (most-recent-first) just after the header.

    Entry format
    ------------
    ---
    **Snapshot:** `PROFIN.B.1.0.md`  |  **Files:** 18  |  **Handler:** v1.0.0
    **Date:** 2025-08-06 14:30  |  **Type:** Beta
    **Description:** Phase 1 complete — DB schema + auth.
    """
    changelog_path = ROOT / "CHANGELOG.md"
    type_label     = "Beta"    if ver_type == "B" else "Release"
    type_tag       = "[Beta]"  if ver_type == "B" else "[Release]"

    new_entry = (
        "\n---\n\n"
        "**Snapshot:** `{}.md`  |  **Files:** {}  |  **Handler:** v{}\n"
        "**Date:** {}  |  **Type:** {}\n"
        "**Description:** {}\n"
    ).format(
        snap_base,
        file_count,
        HANDLER_VERSION,
        now,
        type_label,
        description if description else "(no description)",
    )

    changelog_header = (
        "# PROFIN — Changelog\n\n"
        "> Personal & Project Finance Tracker  \n"
        "> GE Vernova Power Systems Engineering  \n"
        "> Auto-generated by `tools/ExportVersionHandler.py`\n\n"
    )

    if not changelog_path.exists():
        # Bootstrap: create from scratch
        updated = changelog_header + new_entry
    else:
        existing = changelog_path.read_text(encoding="utf-8", errors="replace")
        # Prepend new entry after the header block
        if "---" in existing:
            # Insert before the first --- separator
            idx = existing.find("\n---")
            updated = existing[:idx] + new_entry + existing[idx:]
        else:
            updated = existing + new_entry

    try:
        changelog_path.write_text(updated, encoding="utf-8", newline="\n")
        print("  CHANGELOG.md updated.")
    except OSError as exc:
        print("  WARNING: Could not update CHANGELOG.md: {}".format(exc))


# ==============================================================================
# CONFIG VERSION WRITE-BACK
# ==============================================================================

def _write_version_to_config(new_version: str) -> bool:
    """
    Write the chosen snapshot version back to app/config.py.
    Updates APP_VERSION and APP_RELEASE_DATE.
    Refreshes module-level CURRENT_APP_VERSION global after writing.
    """
    global CURRENT_APP_VERSION
    config_path = ROOT / "app" / "config.py"
    if not config_path.exists():
        print("  WARNING: app/config.py not found -- version NOT written back.")
        return False
    try:
        original_text = config_path.read_text(encoding="utf-8")
        lines         = original_text.splitlines(keepends=True)
    except OSError as exc:
        print("  WARNING: Could not read app/config.py: {}".format(exc))
        return False

    new_lines       = []
    version_written = False
    date_written    = False
    now_date        = datetime.now().strftime("%Y-%m-%d")

    for line in lines:
        stripped = line.rstrip("\n\r")

        # Replace APP_VERSION = "..."
        if re.match(r'^APP_VERSION\s*=', stripped) and not version_written:
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            new_lines.append('APP_VERSION = "{}"{}'.format(new_version, eol))
            version_written = True

        # Replace APP_RELEASE_DATE = "..."
        elif re.match(r'^APP_RELEASE_DATE\s*=', stripped) and not date_written:
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            new_lines.append(
                'APP_RELEASE_DATE = "{}"{}'.format(now_date, eol))
            date_written = True

        else:
            new_lines.append(line)

    if not version_written:
        print("  WARNING: APP_VERSION line not found in app/config.py"
              " -- not updated.")
        return False

    try:
        config_path.write_text(
            "".join(new_lines), encoding="utf-8", newline="\n")
    except OSError as exc:
        print("  WARNING: Could not write app/config.py: {}".format(exc))
        return False

    # Refresh global so the rest of this run uses the new version
    CURRENT_APP_VERSION = new_version
    print('  app/config.py updated -> APP_VERSION = "{}"'.format(new_version))
    if date_written:
        print('  app/config.py updated -> APP_RELEASE_DATE = "{}"'.format(
            now_date))
    else:
        print("  app/config.py: APP_RELEASE_DATE not found -- "
              "add it to app/config.py if needed.")
    return True


# ==============================================================================
# .PRJ ARCHIVE HELPERS
# ==============================================================================

def _write_prj_archive(
    prj_path:   Path,
    file_list:  List[str],
    manifest:   Dict,
) -> None:
    """
    Write a .prj portable archive (ZIP) containing all source files
    and a manifest.json with snapshot metadata.
    """
    with zipfile.ZipFile(prj_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write manifest
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )
        # Write source files
        for rel in file_list:
            src = ROOT / rel
            if src.exists():
                try:
                    zf.write(src, arcname="source/{}".format(rel))
                except Exception as exc:
                    print("  WARNING: Could not archive {}: {}".format(
                        rel, exc))


def _read_prj_manifest(prj_path: Path) -> Optional[Dict]:
    """Read and return the manifest.json from a .prj archive."""
    try:
        with zipfile.ZipFile(prj_path, "r") as zf:
            if "manifest.json" in zf.namelist():
                return json.loads(
                    zf.read("manifest.json").decode("utf-8", errors="replace"))
    except Exception as exc:
        print("  ERROR: Cannot read .prj archive {}: {}".format(
            prj_path.name, exc))
    return None


# ==============================================================================
# MAIN EXPORT FUNCTION
# ==============================================================================

def export_version(
    version:     Optional[str] = None,
    ver_type:    Optional[str] = None,
    output_path: Optional[Path] = None,
    also_prj:    Optional[bool] = None,
) -> None:
    """
    Write all project source files to a versioned markdown snapshot
    stored in ROOT/versions/.

    Parameters
    ----------
    version     : Snapshot label e.g. "1.0". None -> interactive prompt.
    ver_type    : "B" (Beta) or "R" (Release). None -> interactive prompt.
    output_path : Override output .md path (optional, Path object).
    also_prj    : If True, also write a .prj archive. None -> prompt user.
    """
    _ensure_dir(VERSIONS_DIR)

    # -- Version type prompt --------------------------------------------------
    if ver_type is None:
        ver_type = _prompt_version_type()

    # -- Version number prompt ------------------------------------------------
    if version is None:
        version, is_override = _prompt_export_version(ver_type)
    else:
        is_override = False

    # -- Description (new revisions only) -------------------------------------
    description = ""
    if not is_override:
        description = _prompt_revision_description()

    # -- .prj prompt ----------------------------------------------------------
    if also_prj is None:
        also_prj = _prompt_also_prj()

    # -- Build output paths ---------------------------------------------------
    snap_base   = _snapshot_filename(CURRENT_APP_NAME, ver_type, version)
    if output_path is None:
        output_path = VERSIONS_DIR / "{}.md".format(snap_base)
    prj_path = VERSIONS_DIR / "{}.prj".format(snap_base) if also_prj else None

    # -- Overwrite guard ------------------------------------------------------
    if not _check_existing_file(output_path):
        return

    # -- Update version in app/config.py (new revisions only) ----------------
    if not is_override:
        _write_version_to_config(version)

    # -- Build file list ------------------------------------------------------
    ordered, auto_added = _build_file_list()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # -- Read & embed files ---------------------------------------------------
    sections = []
    missing  = []
    present  = []

    for rel in ordered:
        src = ROOT / rel
        if not src.exists():
            missing.append(rel)
            continue
        present.append(rel)
        try:
            code = src.read_text(encoding="utf-8", errors="backslashreplace")
        except Exception as exc:
            code = "# ERROR reading file: {}\n".format(exc)

        ext      = src.suffix.lstrip(".").lower() or "text"
        lang_map = {
            "py":   "python",
            "md":   "markdown",
            "ini":  "ini",
            "cfg":  "ini",
            "toml": "toml",
            "txt":  "text",
            "json": "json",
            "html": "html",
            "bat":  "batch",
            "sh":   "bash",
        }
        lang = lang_map.get(ext, "text")

        # BUG-EH-03 FIX: no trailing space before \n in heading
        sections.append(
            "## `{}`\n\n{}\n\n".format(rel, _escape_fence(code, lang))
        )

    file_count = len(present)

    # -- Table of Contents ----------------------------------------------------
    toc = "\n".join(
        "- [`{}`](#{})".format(
            rel,
            rel.lower()
               .replace("/", "")
               .replace(".", "")
               .replace("_", "-")
               .replace(" ", "-"),
        )
        for rel in present
    )

    # -- Build header notes ---------------------------------------------------
    override_note    = (
        "> **⚠ Override export** — same version re-exported.  \n"
        if is_override else ""
    )
    description_note = (
        "> **Description:** {}  \n".format(description)
        if description else ""
    )
    auto_note = (
        "> **Auto-discovered:** {} extra file(s) appended.  \n".format(
            len([f for f in auto_added if (ROOT / f).exists()]))
        if auto_added else ""
    )

    # -- Assemble header ------------------------------------------------------
    header = (
        "# {} -- Full Python Source Export\n\n"
        "> **Version type:** {} ({})\n"
        "> **Version:** {}  \n"
        "> **App name  (app/config.py):** {}  \n"
        "> **App version (app/config.py):** {}  \n"
        "> **Export date:** {}  \n"
        "> **Files included:** {}  \n"
        "> **Handler version:** {}  \n"
        "> **Generated by:** `tools/ExportVersionHandler.py`  \n"
        "{}"
        "{}"
        "{}"
        "\n---\n\n"
        "## Table of Contents\n\n{}\n\n---\n\n"
    ).format(
        snap_base,
        "Beta" if ver_type == "B" else "Release", ver_type,
        version,
        CURRENT_APP_NAME,
        CURRENT_APP_VERSION,
        now,
        file_count,
        HANDLER_VERSION,
        override_note,
        description_note,
        auto_note,
        toc,
    )

    if missing:
        header += (
            "## WARNING: Missing Files (skipped)\n\n"
            + "\n".join("- `{}`".format(f) for f in missing)
            + "\n\n---\n\n"
        )
    if auto_added:
        present_extras = [f for f in auto_added if (ROOT / f).exists()]
        header += (
            "## Auto-Discovered Files (not in PRIORITY_FILES)\n\n"
            + "\n".join("- `{}`".format(f) for f in present_extras)
            + "\n\n---\n\n"
        )

    # -- Write .md snapshot ---------------------------------------------------
    full_content = header + "".join(sections)
    try:
        output_path.write_text(full_content, encoding="utf-8", newline="\n")
    except OSError as exc:
        print("  ERROR: Could not write snapshot: {}".format(exc))
        return

    size_kb = output_path.stat().st_size / 1024

    # -- Write .prj archive ---------------------------------------------------
    if also_prj and prj_path:
        manifest = {
            "snap_base":    snap_base,
            "ver_type":     ver_type,
            "app_name":     CURRENT_APP_NAME,
            "app_version":  CURRENT_APP_VERSION,
            "export_date":  now,
            "handler":      HANDLER_VERSION,
            "description":  description,
            "is_override":  is_override,
            "files":        present,
        }
        try:
            _write_prj_archive(prj_path, present, manifest)
        except Exception as exc:
            print("  ERROR: Could not write .prj archive: {}".format(exc))

    # -- Update CHANGELOG (new revisions only) --------------------------------
    if not is_override:
        _update_changelog(ver_type, version, now, file_count,
                          description, snap_base)

    # -- Final summary --------------------------------------------------------
    print("\n  {}".format(_sep("=")))
    print("  EXPORT COMPLETE")
    print("  {}".format(_sep("-")))
    print("  Snapshot : {}".format(snap_base))
    print("  Type     : {}".format("Beta" if ver_type == "B" else "Release"))
    print("  Files    : {}  ({} missing, {} auto-added)".format(
        file_count, len(missing), len(auto_added)))
    print("  .md      : {}  ({:.1f} KB)".format(output_path.name, size_kb))
    if prj_path and prj_path.exists():
        prj_kb = prj_path.stat().st_size / 1024
        print("  .prj     : {}  ({:.1f} KB)".format(prj_path.name, prj_kb))
    if not is_override:
        print("  Desc.    : {}".format(description))
        print("  CHANGELOG: CHANGELOG.md updated")
    else:
        print("  Override : CHANGELOG.md NOT modified")
    print("  {}\n".format(_sep("=")))


# ==============================================================================
# IMPORT / INSPECT FUNCTION
# ==============================================================================

def import_snapshot() -> None:
    """
    Interactive viewer: shows metadata and file list from a .md or .prj
    snapshot. For .prj files, optionally extracts to a staging folder.
    """
    _ensure_dir(VERSIONS_DIR)
    existing = _list_existing_versions()
    if not existing:
        print("\n  No snapshots found in versions/ -- nothing to import/view.")
        return

    print("\n  {}".format(_sep()))
    print("  SELECT SNAPSHOT TO INSPECT")
    print(_sep())
    for i, f in enumerate(existing, 1):
        print("  [{:2d}]  {}".format(i, f.name))
    print("  [ 0]  Cancel")
    print(_sep())

    while True:
        raw = input("\n  Enter number: ").strip()
        if raw == "0":
            print("  Cancelled.")
            return
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(existing):
                chosen = existing[idx]
                break
        except ValueError:
            pass
        print("  Please enter a number between 0 and {}.".format(
            len(existing)))

    if chosen.suffix == ".prj":
        manifest = _read_prj_manifest(chosen)
        if manifest is None:
            print("  ERROR: Cannot read manifest.")
            return
        print("\n  {}".format(_sep("=")))
        print("  Snapshot  : {}".format(manifest.get("snap_base", "?")))
        print("  Type      : {}".format(
            "Beta" if manifest.get("ver_type") == "B" else "Release"))
        print("  App ver   : {}".format(manifest.get("app_version", "?")))
        print("  Exported  : {}".format(manifest.get("export_date", "?")))
        print("  Files     : {}".format(len(manifest.get("files", []))))
        desc = manifest.get("description", "")
        if desc:
            print("  Desc.     : {}".format(desc))
        print("  {}".format(_sep("-")))
        for rel in manifest.get("files", []):
            print("    {}".format(rel))
        print("  {}".format(_sep("=")))

        # Offer staging extraction
        print("\n  Extract to staging folder for review? [y/n]: ", end="")
        if input().strip().lower() == "y":
            staging = RESTORE_DIR / "staging_{}".format(
                chosen.stem)
            _ensure_dir(staging)
            try:
                with zipfile.ZipFile(chosen, "r") as zf:
                    zf.extractall(staging)
                print("  Extracted to: {}".format(
                    staging.relative_to(ROOT)))
            except Exception as exc:
                print("  ERROR: Extraction failed: {}".format(exc))
    else:
        # .md file — show first 40 lines (header block)
        try:
            lines = chosen.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print("  ERROR: Cannot read file: {}".format(exc))
            return
        print("\n  {}".format(_sep("=")))
        print("  File: {}".format(chosen.name))
        print("  {}".format(_sep("-")))
        for line in lines[:40]:
            print("  {}".format(line))
        if len(lines) > 40:
            print("  ... [{} more lines]".format(len(lines) - 40))
        print("  {}".format(_sep("=")))


# ==============================================================================
# RESTORE FUNCTION
# ==============================================================================

def restore_snapshot() -> None:
    """Interactive restore from a .prj archive with full backup."""
    _ensure_dir(VERSIONS_DIR)
    existing  = _list_existing_versions()
    prj_files = [f for f in existing if f.suffix == ".prj"]
    if not prj_files:
        print("\n  No .prj archives found in versions/ -- nothing to restore.")
        return

    print("\n  {}".format(_sep()))
    print("  SELECT .prj ARCHIVE TO RESTORE")
    print(_sep())
    for i, f in enumerate(prj_files, 1):
        print("  [{:2d}]  {}".format(i, f.name))
    print("  [ 0]  Cancel")
    print(_sep())

    while True:
        raw = input("\n  Enter number: ").strip()
        if raw == "0":
            print("  Restore cancelled.")
            return
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(prj_files):
                chosen = prj_files[idx]
                break
        except ValueError:
            pass
        print("  Please enter a number between 0 and {}.".format(
            len(prj_files)))

    manifest = _read_prj_manifest(chosen)
    if manifest is None:
        print("  ERROR: Cannot read manifest -- restore aborted.")
        return

    files_to_restore = manifest.get("files",       [])
    snap_base        = manifest.get("snap_base",    "unknown")
    snap_type        = manifest.get("ver_type",     "?")
    snap_app_ver     = manifest.get("app_version",  "unknown")
    snap_date        = manifest.get("export_date",  "unknown")
    snap_desc        = manifest.get("description",  "")

    print("\n  {}".format(_sep("=")))
    print("  Snapshot  : {}".format(snap_base))
    print("  Type      : {}".format(
        "Beta" if snap_type == "B" else "Release"))
    print("  App ver   : {}".format(snap_app_ver))
    print("  Exported  : {}".format(snap_date))
    print("  Files     : {}".format(len(files_to_restore)))
    if snap_desc:
        print("  Desc.     : {}".format(snap_desc))
    print("  {}".format(_sep("-")))

    will_overwrite = []
    will_create    = []
    for rel in files_to_restore:
        if (ROOT / rel).exists():
            will_overwrite.append(rel)
            print("  [OVERWRITE]  {}".format(rel))
        else:
            will_create.append(rel)
            print("  [NEW FILE]   {}".format(rel))

    if will_overwrite:
        print("\n  WARNING: {} file(s) will be OVERWRITTEN.".format(
            len(will_overwrite)))
    if will_create:
        print("  {} new file(s) will be CREATED.".format(len(will_create)))
    print("  All existing files will be backed up to versions/restores/")
    print("  {}".format(_sep("=")))
    print("\n  THIS WILL OVERWRITE LIVE PROJECT FILES.")
    print("  Type  CONFIRM  to proceed, or press Enter to cancel safely.")
    if input("\n  Type CONFIRM to restore: ").strip() != "CONFIRM":
        print("  Restore cancelled -- live project unchanged.")
        return

    # -- Perform restore with backup ------------------------------------------
    backup_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = RESTORE_DIR / "backup_{}".format(backup_ts)
    _ensure_dir(backup_dir)

    restored = 0
    errors   = 0

    try:
        with zipfile.ZipFile(chosen, "r") as zf:
            for rel in files_to_restore:
                dest = ROOT / rel

                # Backup existing file first
                if dest.exists():
                    bak = backup_dir / rel
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(dest, bak)
                    except Exception as exc:
                        print("  WARNING: Backup failed for {}: {}".format(
                            rel, exc))

                # Write restored file
                src_key = "source/{}".format(rel)
                if src_key not in zf.namelist():
                    print("  WARNING: Not found in archive: {}".format(rel))
                    errors += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    dest.write_bytes(zf.read(src_key))
                    print("  RESTORED: {}".format(rel))
                    restored += 1
                except Exception as exc:
                    print("  ERROR: {}: {}".format(rel, exc))
                    errors += 1
    except Exception as exc:
        print("  ERROR: Restore failed: {}".format(exc))
        return

    # -- Write restore manifest -----------------------------------------------
    restore_manifest = {
        "restored_from":  chosen.name,
        "backup_dir":     str(backup_dir.relative_to(ROOT)),
        "restored_at":    backup_ts,
        "files_restored": files_to_restore,
        "files_created":  will_create,
    }
    try:
        (backup_dir / "restore_manifest.json").write_text(
            json.dumps(restore_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
            newline="\n",
        )
    except Exception:
        pass

    print("\n  {}".format(_sep("=")))
    print("  RESTORE COMPLETE")
    print("  Restored: {}  |  Errors: {}".format(restored, errors))
    print("  Backup  : {}".format(backup_dir.relative_to(ROOT)))
    print("  {}\n".format(_sep("=")))


# ==============================================================================
# UNDO RESTORE
# ==============================================================================

def undo_restore() -> None:
    """Revert to the most recent backup set created by restore_snapshot()."""
    if not RESTORE_DIR.exists():
        print("\n  No restores directory found -- nothing to undo.")
        return
    backups = sorted(
        [b for b in RESTORE_DIR.iterdir() if b.is_dir()], reverse=True)
    if not backups:
        print("\n  No backup sets found in versions/restores/ "
              "-- nothing to undo.")
        return

    latest = backups[0]
    print("\n  Most recent backup: {}".format(latest.name))
    manifest_path = latest / "restore_manifest.json"
    if not manifest_path.exists():
        print("  WARNING: No restore_manifest.json found "
              "-- cannot determine files to undo.")
        return
    try:
        restore_manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print("  ERROR: Cannot read restore manifest: {}".format(exc))
        return

    files_to_undo = restore_manifest.get("files_restored", [])
    files_created = restore_manifest.get("files_created",  [])
    print("  Files to revert : {}".format(len(files_to_undo)))
    print("  Files to remove : {}".format(len(files_created)))
    confirm = input("\n  Undo this restore? [y/n]: ").strip().lower()
    if confirm != "y":
        print("  Undo cancelled.")
        return

    undone  = 0
    removed = 0
    errors  = 0

    for rel in files_to_undo:
        src  = latest / rel
        dest = ROOT / rel
        if not src.exists():
            print("  WARNING: Backup not found: {}".format(rel))
            errors += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            print("  REVERTED: {}".format(rel))
            undone += 1
        except Exception as exc:
            print("  ERROR: {}: {}".format(rel, exc))
            errors += 1

    for rel in files_created:
        dest = ROOT / rel
        if dest.exists():
            try:
                dest.unlink()
                print("  REMOVED: {}".format(rel))
                removed += 1
            except Exception as exc:
                print("  WARNING: Could not remove {}: {}".format(rel, exc))

    print("\n  {}".format(_sep("=")))
    print("  UNDO COMPLETE")
    print("  Reverted: {}  |  Removed: {}  |  Errors: {}".format(
        undone, removed, errors))
    print("  {}\n".format(_sep("=")))


# ==============================================================================
# MAIN MENU
# ==============================================================================

def main() -> None:
    """
    CLI entry point for the PROFIN Version Snapshot Handler.
    Run from the project root:
        python tools/ExportVersionHandler.py
    """
    _configure_console_encoding()
    print("\n  {}".format(_sep("=")))
    print("  PROFIN — Version Snapshot Handler  v{}".format(HANDLER_VERSION))
    print("  Personal & Project Finance Tracker")
    print("  {}".format(_sep("=")))
    print("  Project root : {}".format(ROOT))
    print("  App name     : {}".format(CURRENT_APP_NAME))
    print("  App version  : {}".format(CURRENT_APP_VERSION))
    print("  {}".format(_sep("-")))
    print("  [1]  Export  -- create new .md snapshot + .prj archive")
    print("  [2]  Import  -- inspect an existing snapshot")
    print("  [3]  Restore -- apply a .prj archive to live project")
    print("  [4]  Undo    -- revert the last restore operation")
    print("  [0]  Exit")
    print("  {}".format(_sep("=")))

    while True:
        choice = input("\n  Select an option [0-4]: ").strip()
        if choice == "1":
            export_version()
            break
        elif choice == "2":
            import_snapshot()
            break
        elif choice == "3":
            restore_snapshot()
            break
        elif choice == "4":
            undo_restore()
            break
        elif choice == "0":
            print("  Exiting.\n")
            break
        else:
            print("  Please enter 0, 1, 2, 3, or 4.")


if __name__ == "__main__":
    main()