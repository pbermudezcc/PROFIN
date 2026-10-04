"""
app/web/app.py
==============
FAMILYFIN — FastAPI application factory for the LAN web front-end.

Pages are rendered on the server (Jinja2) and enhanced with htmx; charts
read JSON from /api/… endpoints. No docs/OpenAPI endpoints, no third-party
origins, generic error pages.
"""
from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.gui._format import fmt_date, fmt_eur, fmt_number, fmt_pct, fmt_quantity
from app.web.deps import CsrfFailed, LoginRequired
from app.web.security import HardeningMiddleware, LoginLimiter, SessionStore
from app.web.uploads import PendingImports

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

_MONTHS = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
_MONTHS_LONG = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _short_date(value: Optional[date]) -> str:
    return f"{value.day} {_MONTHS[value.month]}" if value else "—"


def _long_date(value: Optional[date]) -> str:
    return f"{value.day} de {_MONTHS_LONG[value.month]} de {value.year}" if value else "—"


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


@lru_cache(maxsize=None)
def asset(path: str) -> str:
    """/static URL with a content hash, so phones never keep a stale copy
    (the service worker caches static files)."""
    digest = hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.autoescape = True
    env.globals["asset"] = asset
    env.filters.update(
        eur=fmt_eur,
        num=fmt_number,
        pct=fmt_pct,
        qty=fmt_quantity,
        fdate=fmt_date,
        sdate=_short_date,
        ldate=_long_date,
        tojson_attr=lambda value: json.dumps(value, ensure_ascii=False, default=str),
    )
    return templates


def create_app(
    *,
    sessions: Optional[SessionStore] = None,
    limiter: Optional[LoginLimiter] = None,
    on_change: Optional[Callable[[int, str], None]] = None,
    lan_only: bool = True,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions or SessionStore()
    app.state.limiter = limiter or LoginLimiter()
    app.state.on_change = on_change
    # Uploaded statements waiting for confirmation (temp files on this PC).
    # WebServer.stop() clears it (deletes the temp files).
    app.state.imports = PendingImports()
    app.state.templates = build_templates()

    app.add_middleware(HardeningMiddleware, lan_only=lan_only)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> Response:
        if _wants_json(request):
            return JSONResponse({"detail": "Sesión caducada"}, status_code=401)
        if _is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(CsrfFailed)
    async def _csrf_failed(request: Request, _exc: CsrfFailed) -> Response:
        logger.warning("Web: petición sin token CSRF válido (%s %s)", request.method, request.url.path)
        return JSONResponse({"detail": "Petición no válida"}, status_code=403)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if _wants_json(request) or _is_htmx(request):
            return JSONResponse({"detail": exc.detail if exc.status_code < 500 else "Error"},
                                status_code=exc.status_code)
        return app.state.templates.TemplateResponse(
            request, "error.html", {"status": exc.status_code}, status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, _exc: RequestValidationError) -> Response:
        return JSONResponse({"detail": "Datos no válidos"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        logger.exception("Web: error inesperado en %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Error interno"}, status_code=500)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def _manifest() -> Response:
        return Response((STATIC_DIR / "manifest.webmanifest").read_bytes(),
                        media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def _service_worker() -> Response:
        # Served from the root so its scope covers the whole app.
        return Response((STATIC_DIR / "js" / "sw.js").read_bytes(),
                        media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/"})

    from app.web.routers import admin, auth, dashboard, flows, imports, investments, more, transactions

    for router in (auth, dashboard, flows, transactions, investments, admin, imports, more):
        app.include_router(router.router)
    return app
