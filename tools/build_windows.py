#!/usr/bin/env python3
"""Build a Windows executable for PROFIN with a traceable artifact name."""

from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the PROFIN Windows executable")
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

    print(f"Compilando {artifact_name} ...")
    print("Comando:", " ".join(command))
    build_environment = os.environ.copy()
    build_environment["PROFIN_CHANNEL"] = channel[0].upper()
    subprocess.run(command, cwd=ROOT, check=True, env=build_environment)
    exe_path = dist_dir / artifact_name / f"{artifact_name}.exe"
    print(f"Artefacto generado: {exe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
