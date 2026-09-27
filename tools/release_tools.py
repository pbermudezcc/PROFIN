#!/usr/bin/env python3
"""
tools/release_tools.py
=======================
FAMILYFIN — mechanical checks and helpers behind the `release` skill.

The skill (.claude/skills/release/SKILL.md) drives the release; this module
does the parts that must be exact and repeatable:

  preflight     tests + repo/version/remote checks before touching anything
  draft         release-notes draft built from git: every commit since the
                last tag, every changed area, schema migrations, agent log
  verify-notes  refuse notes that leave TODOs or miss a commit/area/migration
  package       zip the whole PyInstaller one-dir build (exe + _internal)
  postflight    CHANGELOG / config / snapshot / tag and, after publishing,
                the GitHub Release body and assets match what was intended

Usage:
  python tools/release_tools.py preflight   --version 0.4.0 [--channel R|B] [--skip-tests]
  python tools/release_tools.py draft       --version 0.4.0 [--out notes.md]
  python tools/release_tools.py verify-notes notes.md --version 0.4.0
  python tools/release_tools.py package     --version 0.4.0 [--channel R|B]
  python tools/release_tools.py postflight  --version 0.4.0 --notes notes.md [--channel R|B] [--published]

Every command exits 0 when all checks pass and 1 otherwise, printing each
problem. Nothing here pushes, tags or publishes.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "FAMILYFIN"
BUILD_NAME = "PROFIN"          # PyInstaller artifact prefix (kept by design)

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?$")

# Files that must never be tracked: real family data, secrets, build output.
FORBIDDEN_TRACKED = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"\.db(-wal|-shm)?$"),
    re.compile(r"\.zip$"),
    re.compile(r"^(data|logs|build|dist)/"),
]

# Changed paths → release-notes area. First match wins. Areas marked
# user_facing must be described for the family; the others go under "Interno".
@dataclass(frozen=True)
class Area:
    key: str
    label: str
    prefixes: Tuple[str, ...]
    user_facing: bool = True


AREAS: Tuple[Area, ...] = (
    Area("db", "Base de datos y migraciones", ("app/db/",)),
    Area("categorization", "Categorías y reglas", ("app/categorization/",)),
    Area("ingestion", "Importación de extractos", ("app/ingestion/",)),
    Area("analytics", "Informes y gráficos", ("app/analytics/",)),
    Area("settings", "Configuración y copias de seguridad", ("app/settings/",)),
    Area("auth", "Usuarios y acceso", ("app/auth/",)),
    Area("gui", "Interfaz", ("app/gui/", "app/config.py", "main.py")),
    Area("build", "Instalación y compilación Windows",
         ("build_profin.spec", "build_profin.bat", "tools/build_windows.py", "requirements.txt")),
    Area("tests", "Tests", ("tests/", "conftest.py"), user_facing=False),
    Area("tooling", "Herramientas de desarrollo y agentes",
         ("tools/", ".claude/", ".github/", ".vscode/", ".gitignore"), user_facing=False),
    Area("docs", "Documentación", ("docs/",), user_facing=False),
)

# Release bookkeeping, never a "change" in itself.
IGNORED_PATHS = (re.compile(r"^CHANGELOG\.md$"), re.compile(r"^versions/"))

COMMIT_SECTIONS = (
    ("Novedades", ("feat",)),
    ("Correcciones", ("fix",)),
    ("Mejoras", ("perf", "refactor", "style", "ui")),
    ("Interno", ("chore", "test", "tests", "docs", "build", "ci")),
)

DRAFT_MARKERS = ("TODO", "BORRADOR")


Runner = Callable[[Sequence[str]], str]


def _run(args: Sequence[str], root: Path = ROOT) -> str:
    result = subprocess.run(
        list(args), cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} falló ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def git(*args: str, root: Path = ROOT) -> str:
    return _run(["git", *args], root)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def parse_version(version: str) -> Tuple[int, int, int, int]:
    """(major, minor, patch, beta) — beta = large number for a final release."""
    match = VERSION_RE.match(version)
    if not match:
        raise ValueError(f"Versión no válida '{version}' (se espera X.Y.Z o X.Y.Z-beta.N)")
    major, minor, patch, beta = match.groups()
    return int(major), int(minor), int(patch), int(beta) if beta else 10**9


def read_config_value(key: str, root: Path = ROOT) -> Optional[str]:
    text = (root / "app" / "config.py").read_text(encoding="utf-8")
    match = re.search(rf'^{key}\s*=\s*["\']([^"\']*)["\']', text, re.MULTILINE)
    return match.group(1) if match else None


def snapshot_base(version: str, channel: str) -> str:
    return f"{APP_NAME}.{channel}.{version}"


def build_dir(version: str, channel: str, root: Path = ROOT) -> Path:
    return root / "dist" / f"{BUILD_NAME}.{channel}.{version}"


def build_zip(version: str, channel: str, root: Path = ROOT) -> Path:
    return root / "dist" / f"{BUILD_NAME}.{channel}.{version}-windows.zip"


def last_release_tag(root: Path = ROOT) -> Optional[str]:
    """Newest v* tag reachable from HEAD, or None."""
    try:
        return git("describe", "--tags", "--abbrev=0", "--match", "v*", root=root).strip() or None
    except RuntimeError:
        return None


def schema_version_at(ref: Optional[str], root: Path = ROOT) -> int:
    """Highest SCHEMA_MIGRATIONS step in app/db/database.py at ref (None = working tree)."""
    try:
        if ref is None:
            text = (root / "app" / "db" / "database.py").read_text(encoding="utf-8")
        else:
            text = git("show", f"{ref}:app/db/database.py", root=root)
    except (OSError, RuntimeError):
        return 0
    block = re.search(r"SCHEMA_MIGRATIONS\s*=\s*\((.*?)\n\)", text, re.DOTALL)
    if not block:
        return 0
    steps = [int(n) for n in re.findall(r"\(\s*(\d+)\s*,", block.group(1))]
    return max(steps, default=0)


# ---------------------------------------------------------------------------
# Changes since the last release
# ---------------------------------------------------------------------------

@dataclass
class Commit:
    sha: str
    subject: str

    @property
    def kind(self) -> str:
        match = re.match(r"^(\w+)(\([^)]*\))?!?:", self.subject)
        return match.group(1).lower() if match else ""

    @property
    def is_release_bookkeeping(self) -> bool:
        return self.subject.startswith("chore(release)")


@dataclass
class Changes:
    since_tag: Optional[str]
    commits: List[Commit] = field(default_factory=list)
    files_by_area: Dict[str, List[str]] = field(default_factory=dict)
    schema_from: int = 0
    schema_to: int = 0
    log_entries: List[str] = field(default_factory=list)

    @property
    def schema_changed(self) -> bool:
        return self.schema_to > self.schema_from

    def areas(self) -> List[Area]:
        return [a for a in AREAS if a.key in self.files_by_area]


def area_for(path: str) -> Optional[Area]:
    if any(p.search(path) for p in IGNORED_PATHS):
        return None
    for area in AREAS:
        if any(path == pre or path.startswith(pre) for pre in area.prefixes):
            return area
    return None


def _config_only_version_bump(diff: str) -> bool:
    changed = [
        line for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    return all(re.match(r"^[+-]\s*(APP_VERSION|APP_RELEASE_DATE)\s*=", l) for l in changed)


def agent_log_entries_since(day: Optional[str], root: Path = ROOT) -> List[str]:
    """Titles ('YYYY-MM-DD — title') of docs/agent/agent_log.md entries on/after day."""
    path = root / "docs" / "agent" / "agent_log.md"
    if not path.exists():
        return []
    titles = re.findall(r"^## (\d{4}-\d{2}-\d{2}) — (.+)$", path.read_text(encoding="utf-8"), re.MULTILINE)
    return [f"{d} — {t}" for d, t in titles if day is None or d >= day]


def collect_changes(root: Path = ROOT) -> Changes:
    tag = last_release_tag(root)
    rev_range = f"{tag}..HEAD" if tag else "HEAD"
    changes = Changes(since_tag=tag)

    log = git("log", "--no-merges", "--format=%h%x09%s", rev_range, root=root)
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        commit = Commit(sha, subject.strip())
        if not commit.is_release_bookkeeping:
            changes.commits.append(commit)

    if tag:
        names = git("diff", "--name-only", f"{tag}..HEAD", root=root).splitlines()
    else:
        names = git("ls-files", root=root).splitlines()
    for path in names:
        if path == "app/config.py" and tag:
            if _config_only_version_bump(git("diff", f"{tag}..HEAD", "--", path, root=root)):
                continue
        area = area_for(path)
        if area:
            changes.files_by_area.setdefault(area.key, []).append(path)

    changes.schema_from = schema_version_at(tag, root) if tag else 0
    changes.schema_to = schema_version_at("HEAD", root)
    tag_day = git("log", "-1", "--format=%cs", tag, root=root).strip() if tag else None
    changes.log_entries = agent_log_entries_since(tag_day, root)
    return changes


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------

def migration_sentence(schema_from: int, schema_to: int) -> str:
    return (
        f"La base de datos se actualiza automáticamente del esquema v{schema_from} "
        f"al v{schema_to} la primera vez que se abre la aplicación. Antes se guarda "
        f"una copia `profin.pre-migration-v{schema_to}.<fecha>.db` junto a la base."
    )


def draft_notes(changes: Changes, version: str) -> str:
    """Markdown draft: every fact is filled in, prose is left as TODO."""
    out: List[str] = [
        "<!-- BORRADOR: redacta los TODO para la familia (en español, sin jerga), "
        "no borres 'Áreas afectadas' ni 'Commits incluidos', y elimina este comentario "
        "y el de 'Fuentes' antes de publicar. -->",
        "",
        "### Resumen",
        "",
        f"TODO: 2-4 frases sobre qué cambia al actualizar a {version}.",
        "",
    ]
    if changes.schema_changed:
        out += ["### Antes de actualizar", "", "- " + migration_sentence(changes.schema_from, changes.schema_to), ""]

    by_section: Dict[str, List[Commit]] = {name: [] for name, _ in COMMIT_SECTIONS}
    unclassified: List[Commit] = []
    for commit in changes.commits:
        for name, kinds in COMMIT_SECTIONS:
            if commit.kind in kinds:
                by_section[name].append(commit)
                break
        else:
            unclassified.append(commit)
    by_section["Mejoras"] += unclassified
    for name, _ in COMMIT_SECTIONS:
        if by_section[name]:
            out += [f"### {name}", ""]
            out += [f"- TODO: {c.subject} (`{c.sha}`)" for c in by_section[name]]
            out.append("")

    out += ["### Áreas afectadas", ""]
    for area in changes.areas():
        files = changes.files_by_area[area.key]
        shown = ", ".join(f"`{f}`" for f in files[:6]) + (f" y {len(files) - 6} más" if len(files) > 6 else "")
        out.append(f"- **{area.label}**: {shown}")
    out.append("")

    out += [
        "<details><summary>Commits incluidos "
        f"({len(changes.commits)}{' desde ' + changes.since_tag if changes.since_tag else ''})</summary>",
        "",
    ]
    out += [f"- `{c.sha}` {c.subject}" for c in changes.commits]
    out += ["", "</details>", ""]

    if changes.log_entries:
        out += ["<!-- Fuentes para redactar (docs/agent/agent_log.md):"]
        out += [f"  {entry}" for entry in changes.log_entries]
        out += ["-->", ""]
    return "\n".join(out)


def split_notes(notes: str) -> Tuple[str, str]:
    """(summary paragraph, rest of the notes) — summary feeds CHANGELOG 'Description'."""
    match = re.search(r"^### Resumen\s*\n(.*?)(?=^### |\Z)", notes, re.MULTILINE | re.DOTALL)
    if not match:
        return "", notes.strip()
    summary = " ".join(match.group(1).split())
    rest = (notes[: match.start()] + notes[match.end():]).strip()
    return summary, rest


def verify_notes(notes: str, changes: Changes) -> List[str]:
    """Problems that make the notes unfit to publish (empty list = OK)."""
    problems: List[str] = []
    for marker in DRAFT_MARKERS:
        if marker in notes:
            problems.append(f"Quedan marcas de borrador '{marker}' en las notas.")
    if "<!--" in notes:
        problems.append("Quedan comentarios HTML (<!-- -->) en las notas.")
    for line in notes.splitlines():
        if line.strip() == "---":
            problems.append("Las notas contienen una línea '---' (rompe el CHANGELOG).")
            break
    summary, _ = split_notes(notes)
    if len(summary) < 40:
        problems.append("Falta la sección '### Resumen' o es demasiado corta.")
    for commit in changes.commits:
        if commit.sha not in notes:
            problems.append(f"Falta el commit {commit.sha} ({commit.subject}).")
    for area in changes.areas():
        if area.label not in notes:
            problems.append(f"Falta el área afectada '{area.label}'.")
    if changes.schema_changed and migration_sentence(changes.schema_from, changes.schema_to) not in notes:
        problems.append(
            f"El esquema pasa de v{changes.schema_from} a v{changes.schema_to} y las "
            "notas no incluyen el aviso de migración generado por el borrador."
        )
    return problems


# ---------------------------------------------------------------------------
# Preflight / packaging / postflight
# ---------------------------------------------------------------------------

def forbidden_tracked_files(root: Path = ROOT) -> List[str]:
    tracked = git("ls-files", root=root).splitlines()
    return [f for f in tracked if any(p.search(f) for p in FORBIDDEN_TRACKED)]


def preflight(version: str, channel: str, root: Path = ROOT, run_tests: bool = True) -> List[str]:
    problems: List[str] = []
    try:
        new = parse_version(version)
    except ValueError as exc:
        return [str(exc)]
    if channel == "R" and new[3] != 10**9:
        problems.append("Una release estable (R) no lleva sufijo -beta.N.")
    if channel == "B" and new[3] == 10**9:
        problems.append("Una pre-release (B) debe llevar sufijo -beta.N.")

    current = read_config_value("APP_VERSION", root) or "0.0.0"
    try:
        if new <= parse_version(current):
            problems.append(f"La versión {version} no es mayor que la actual {current}.")
    except ValueError:
        problems.append(f"APP_VERSION actual no es semver: {current}.")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    top = re.search(r"\*\*Snapshot:\*\* `[A-Z]+\.[BR]\.([^`]+)\.md`", changelog)
    if not top or top.group(1) != current:
        problems.append(
            f"La última entrada del CHANGELOG ({top.group(1) if top else 'ninguna'}) "
            f"no coincide con APP_VERSION {current}."
        )

    status = git("status", "--porcelain", root=root)
    if status.strip():
        problems.append("El árbol de trabajo tiene cambios sin commitear:\n" + status.rstrip())
    bad = forbidden_tracked_files(root)
    if bad:
        problems.append("Ficheros prohibidos bajo control de versiones: " + ", ".join(bad))

    tag = f"v{version}"
    if git("tag", "-l", tag, root=root).strip():
        problems.append(f"El tag {tag} ya existe en local.")
    try:
        git("fetch", "--tags", "origin", root=root)
        if git("ls-remote", "--tags", "origin", f"refs/tags/{tag}", root=root).strip():
            problems.append(f"El tag {tag} ya existe en GitHub.")
        branch = git("rev-parse", "--abbrev-ref", "HEAD", root=root).strip()
        if channel == "R" and branch != "main":
            problems.append(f"Una release estable se hace desde 'main' (rama actual: {branch}).")
        upstream = f"origin/{branch}"
        if git("ls-remote", "--heads", "origin", branch, root=root).strip():
            behind = git("rev-list", "--count", f"HEAD..{upstream}", root=root).strip()
            if behind != "0":
                problems.append(f"'{branch}' va {behind} commit(s) por detrás de {upstream}.")
    except RuntimeError as exc:
        problems.append(f"No se pudo consultar GitHub: {exc}")

    try:
        _run(["gh", "auth", "status"], root)
    except (RuntimeError, OSError) as exc:
        problems.append(f"gh no está autenticado: {exc}")

    if run_tests:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        tail = "\n".join(result.stdout.strip().splitlines()[-3:])
        if result.returncode != 0:
            problems.append("Los tests fallan:\n" + tail)
        else:
            print(f"  tests: {tail.splitlines()[-1] if tail else 'ok'}")
    return problems


def package_build(version: str, channel: str, root: Path = ROOT) -> Path:
    """Zip the whole one-dir build (the exe alone cannot run without _internal)."""
    folder = build_dir(version, channel, root)
    exe = folder / f"{folder.name}.exe"
    marker = folder.parent / f"{folder.name}.build-ok"
    if not marker.is_file():
        raise FileNotFoundError(
            f"No hay constancia de que la compilación de {folder.name} terminara bien "
            f"(falta {marker.name}); una compilación fallida puede dejar la carpeta a medias. "
            f"Ejecuta: python tools/build_windows.py --channel {'release' if channel == 'R' else 'beta'}"
        )
    if not exe.is_file() or not (folder / "_internal").is_dir():
        raise FileNotFoundError(
            f"No existe una compilación completa en {folder} (falta el .exe o _internal). "
            f"Ejecuta: python tools/build_windows.py --channel {'release' if channel == 'R' else 'beta'}"
        )
    target = build_zip(version, channel, root)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=f"{folder.name}/{path.relative_to(folder).as_posix()}")
    return target


def _normalise(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines())


def postflight(
    version: str,
    channel: str,
    notes: str,
    root: Path = ROOT,
    published: bool = False,
    runner: Optional[Runner] = None,
) -> List[str]:
    problems: List[str] = []
    if read_config_value("APP_VERSION", root) != version:
        problems.append(f"app/config.py APP_VERSION no es {version}.")
    if read_config_value("APP_RELEASE_DATE", root) != date.today().isoformat():
        problems.append("app/config.py APP_RELEASE_DATE no es la fecha de hoy.")

    base = snapshot_base(version, channel)
    for suffix in (".md", ".prj"):
        if not (root / "versions" / f"{base}{suffix}").is_file():
            problems.append(f"Falta el snapshot versions/{base}{suffix}.")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = changelog.split("\n---\n")
    top = entries[1] if len(entries) > 1 else ""
    if f"`{base}.md`" not in top:
        problems.append(f"La primera entrada del CHANGELOG no es {base}.")
    summary, body = split_notes(notes)
    if summary and summary not in _normalise(top).replace("\n", " "):
        problems.append("El resumen de las notas no está en la entrada del CHANGELOG.")
    if body and _normalise(body) not in _normalise(top):
        problems.append("El cuerpo de las notas no está completo en la entrada del CHANGELOG.")

    tag = f"v{version}"
    try:
        if git("cat-file", "-t", tag, root=root).strip() != "tag":
            problems.append(f"{tag} no es un tag anotado.")
        elif git("rev-list", "-n", "1", tag, root=root).strip() != git("rev-parse", "HEAD", root=root).strip():
            problems.append(f"{tag} no apunta al commit actual.")
        elif _normalise(git("tag", "-l", "--format=%(contents)", tag, root=root)) != _normalise(notes):
            problems.append(
                f"El mensaje del tag {tag} no es el fichero de notas "
                "(¿se creó sin --cleanup=verbatim -F <notas>?)."
            )
    except RuntimeError:
        problems.append(f"No existe el tag {tag}.")

    if not build_zip(version, channel, root).is_file():
        problems.append(f"Falta el paquete {build_zip(version, channel, root).name} (python tools/release_tools.py package).")

    if published:
        run = runner or (lambda args: _run(args, root))
        try:
            info = json.loads(run([
                "gh", "release", "view", tag, "--json",
                "tagName,body,isDraft,isPrerelease,assets",
            ]))
        except (RuntimeError, ValueError) as exc:
            return problems + [f"No se pudo leer la release {tag} en GitHub: {exc}"]
        if info.get("isDraft"):
            problems.append("La release está como borrador (draft).")
        if info.get("isPrerelease") != (channel == "B"):
            problems.append("El flag de pre-release no coincide con el canal.")
        if _normalise(info.get("body", "")) != _normalise(notes):
            problems.append("El texto de la release en GitHub no es igual al fichero de notas.")
        assets = {a.get("name") for a in info.get("assets", [])}
        if build_zip(version, channel, root).name not in assets:
            problems.append(f"La release no tiene adjunto {build_zip(version, channel, root).name}.")
    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report(problems: List[str], ok_message: str) -> int:
    if problems:
        print("PROBLEMAS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(ok_message)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_version(p):
        p.add_argument("--version", required=True)
        p.add_argument("--channel", choices=("R", "B"), default="R")
        return p

    p_pre = with_version(sub.add_parser("preflight"))
    p_pre.add_argument("--skip-tests", action="store_true")
    p_draft = with_version(sub.add_parser("draft"))
    p_draft.add_argument("--out")
    p_verify = with_version(sub.add_parser("verify-notes"))
    p_verify.add_argument("notes")
    with_version(sub.add_parser("package"))
    p_post = with_version(sub.add_parser("postflight"))
    p_post.add_argument("--notes", required=True)
    p_post.add_argument("--published", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "preflight":
        return _report(preflight(args.version, args.channel, run_tests=not args.skip_tests), "Preflight OK.")
    if args.command == "draft":
        text = draft_notes(collect_changes(), args.version)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"Borrador escrito en {args.out}")
        else:
            print(text)
        return 0
    if args.command == "verify-notes":
        notes = Path(args.notes).read_text(encoding="utf-8")
        return _report(verify_notes(notes, collect_changes()), "Notas OK.")
    if args.command == "package":
        print(f"Paquete: {package_build(args.version, args.channel)}")
        return 0
    if args.command == "postflight":
        notes = Path(args.notes).read_text(encoding="utf-8")
        return _report(
            postflight(args.version, args.channel, notes, published=args.published),
            "Postflight OK.",
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
