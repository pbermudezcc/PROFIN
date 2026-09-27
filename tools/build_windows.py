#!/usr/bin/env python3
"""Build a Windows executable for FAMILYFIN with a traceable artifact name."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "build_profin.spec"


def read_version() -> str:
    config_path = ROOT / "app" / "config.py"
    text = config_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("APP_VERSION"):
            value = stripped.split("=", 1)[1].strip()
            return value.strip("\"'")
    raise RuntimeError("No se pudo leer la versión de APP_VERSION en app/config.py")


def build_marker(dist_dir: Path, artifact_name: str) -> Path:
    """dist/<artifact>.build-ok — proof that the last build of this artifact completed."""
    return dist_dir / f"{artifact_name}.build-ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the FAMILYFIN Windows executable")
    parser.add_argument(
        "--channel",
        choices=("beta", "release"),
        default="beta",
        help="Tipo de paquete: beta o release",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Versión opcional (por defecto se toma app/config.py)",
    )
    args = parser.parse_args()

    version = args.version or read_version()
    channel = args.channel.lower()
    artifact_name = f"PROFIN.{channel[0].upper()}.{version}"
    build_dir = ROOT / "build"
    dist_dir = ROOT / "dist"
    build_dir.mkdir(exist_ok=True)
    dist_dir.mkdir(exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--workpath",
        str(build_dir),
        "--distpath",
        str(dist_dir),
        str(SPEC_PATH),
    ]

    # The marker is written only after PyInstaller succeeds, so a crashed
    # build that left a partial dist folder can never be packaged/published
    # (tools/release_tools.py package requires it).
    marker = build_marker(dist_dir, artifact_name)
    marker.unlink(missing_ok=True)

    print(f"Compilando {artifact_name} ...")
    print("Comando:", " ".join(command))
    build_environment = os.environ.copy()
    build_environment["PROFIN_CHANNEL"] = channel[0].upper()
    subprocess.run(command, cwd=ROOT, check=True, env=build_environment)
    exe_path = dist_dir / artifact_name / f"{artifact_name}.exe"
    if not exe_path.is_file():
        raise RuntimeError(f"PyInstaller terminó sin generar {exe_path}")
    marker.write_text(
        json.dumps({"artifact": artifact_name, "version": version, "channel": channel[0].upper()}),
        encoding="utf-8",
    )
    print(f"Artefacto generado: {exe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
