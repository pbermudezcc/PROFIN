"""Manual de usuario (docs/manual) served by the web front-end.

* GET /ayuda/               the manual page
* GET /ayuda/img/{name}     its screenshots
* GET /ayuda/manual.pdf     the same manual as a PDF (also under its file
                            name, which the page's «PDF» link uses so it
                            works from the desktop's file:// too)

Public on purpose (also linked from /login): the manual only shows the
demo data. The page is a self-contained document with an inline <style>
and <script>, so it carries its own CSP instead of the app's (the
hardening middleware keeps headers a response already sets): the script
is allowed by its hash, never by 'unsafe-inline'.
"""
from __future__ import annotations

import base64
import hashlib
import re
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.manual import MANUAL_IMAGES, MANUAL_INDEX, MANUAL_PDF

router = APIRouter()

_IMAGE_NAME = re.compile(r"^[\w.-]+\.png$")
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
_BRAND = '<div class="brand">'
# The manual's header logo leads back to the app.
_BRAND_LINK = '<a class="brand" href="/" title="Volver a FAMILYFIN" style="text-decoration:none">'
# The header's download link, dropped when no PDF has been printed.
_PDF_LINK = re.compile(r'\s*<a class="pdf-dl"[^>]*>.*?</a>', re.S)


@lru_cache(maxsize=1)
def _manual(mtime: float, has_pdf: bool) -> tuple[bytes, str]:
    html = MANUAL_INDEX.read_text(encoding="utf-8")
    if not has_pdf:
        html = _PDF_LINK.sub("", html, count=1)
    head, sep, tail = html.partition(_BRAND)
    if sep:
        html = head + _BRAND_LINK + tail.replace("</div>", "</a>", 1)
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"
        for body in _SCRIPT.findall(html)
    )
    csp = (
        "default-src 'self'; "
        f"script-src {hashes or chr(39) + 'none' + chr(39)}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data:; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'none'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )
    return html.encode("utf-8"), csp


@router.get("/ayuda", include_in_schema=False)
def help_redirect() -> Response:
    # The page's images are relative (img/...): they need the trailing slash.
    return RedirectResponse("/ayuda/", status_code=303)


@router.get("/ayuda/")
def help_page() -> Response:
    if not MANUAL_INDEX.is_file():
        raise HTTPException(status_code=404)
    body, csp = _manual(MANUAL_INDEX.stat().st_mtime, MANUAL_PDF.is_file())
    return Response(body, media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": csp})


@router.get("/ayuda/img/{name}")
def help_image(name: str) -> Response:
    path = MANUAL_IMAGES / name
    if not _IMAGE_NAME.match(name) or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@router.get("/ayuda/manual.pdf")
@router.get(f"/ayuda/{MANUAL_PDF.name}", include_in_schema=False)
def help_pdf() -> Response:
    if not MANUAL_PDF.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(MANUAL_PDF, media_type="application/pdf", filename=MANUAL_PDF.name)
