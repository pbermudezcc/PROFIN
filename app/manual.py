"""
app/manual.py
=============
FAMILYFIN — Where the user manual lives (docs/manual: index.html, img/, PDF).

Resolved from this file, so it works from the source tree and from the
frozen build (build_profin.spec copies docs/manual next to app/).
"""
from pathlib import Path

MANUAL_DIR = Path(__file__).resolve().parents[1] / "docs" / "manual"
MANUAL_INDEX = MANUAL_DIR / "index.html"
MANUAL_IMAGES = MANUAL_DIR / "img"
MANUAL_PDF = MANUAL_DIR / "FAMILYFIN_Manual_de_usuario.pdf"
