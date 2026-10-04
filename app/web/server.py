"""
app/web/server.py
=================
FAMILYFIN — Runs the LAN web front-end inside the desktop app.

WebServer (one per process, see get_web_server()):
  * HTTPS only: uvicorn in a daemon thread with the certificate from
    app/web/tls.py, TLS 1.2 minimum (1.3 preferred), ECDHE + AEAD ciphers.
  * Optional onboarding listener: plain HTTP on another port for at most
    ONBOARDING_SECONDS, serving nothing but the CA profile and the steps to
    trust it on an iPhone/iPad. It never serves app data.
  * on_change: callback (user_id, area) the desktop uses to reload its
    views after a write from the web. Called from the server thread.

Settings live in QSettings under web/… (enabled, port).
"""
from __future__ import annotations

import html
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional

from app.web.security import LoginLimiter, SessionStore
from app.web.tls import TlsMaterial, ensure_tls, host_names, is_lan_address, mobileconfig

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
ONBOARDING_PORT_OFFSET = 1          # 8766
ONBOARDING_SECONDS = 10 * 60
TLS12_CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!DSS"

SETTINGS_ORG = "FAMILYFIN"
SETTINGS_APP = "FAMILYFIN"
KEY_ENABLED = "web/enabled"
KEY_PORT = "web/port"


@dataclass
class WebSettings:
    enabled: bool = False
    port: int = DEFAULT_PORT


def load_web_settings() -> WebSettings:
    from PySide6.QtCore import QSettings
    s = QSettings(SETTINGS_ORG, SETTINGS_APP)
    port = s.value(KEY_PORT, DEFAULT_PORT, type=int)
    return WebSettings(
        enabled=s.value(KEY_ENABLED, False, type=bool),
        port=port if 1024 <= port <= 65000 else DEFAULT_PORT,
    )


def save_web_settings(settings: WebSettings) -> None:
    from PySide6.QtCore import QSettings
    s = QSettings(SETTINGS_ORG, SETTINGS_APP)
    s.setValue(KEY_ENABLED, settings.enabled)
    s.setValue(KEY_PORT, settings.port)
    s.sync()


def harden_ssl_context(context: ssl.SSLContext) -> ssl.SSLContext:
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(TLS12_CIPHERS)       # TLS 1.3 suites are always AEAD
    context.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE | ssl.OP_NO_RENEGOTIATION
    return context


# ---------------------------------------------------------------------------
# Onboarding listener (CA profile only)
# ---------------------------------------------------------------------------

_ONBOARDING_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FAMILYFIN · Configurar este dispositivo</title>
<style>
body{{font:17px -apple-system,system-ui,sans-serif;margin:0;padding:24px;background:#F3F5FA;color:#0F172A}}
main{{max-width:520px;margin:auto}} h1{{font-size:26px}} ol{{padding-left:22px;line-height:1.55}}
li{{margin-bottom:12px}} .btn{{display:block;text-align:center;background:#2563EB;color:#fff;padding:15px;
border-radius:12px;text-decoration:none;font-weight:600;margin:20px 0}} code{{font-size:12px;word-break:break-all;
background:#E8EDF5;padding:6px;border-radius:6px;display:block}} .ok{{color:#16A34A;font-weight:600}}
</style></head><body><main>
<h1>Conexión segura con FAMILYFIN</h1>
<p>Instala una sola vez el certificado de casa para abrir FAMILYFIN con cifrado HTTPS.</p>
<a class="btn" href="/familyfin-ca.mobileconfig">1 · Descargar el perfil</a>
<ol>
<li>Pulsa <b>Permitir</b> y luego abre <b>Ajustes</b> → <b>Perfil descargado</b> → <b>Instalar</b>.</li>
<li>Ve a <b>Ajustes → General → Información → Ajustes de confianza de certificados</b> y activa <b>FAMILYFIN Home CA</b>.</li>
<li>Abre <a href="{url}">{url}</a> y añádela a la pantalla de inicio (Compartir → Añadir a pantalla de inicio).</li>
</ol>
<p>Comprueba que la huella coincide con la que muestra el PC:</p>
<code>{fingerprint}</code>
<p class="ok">Esta página solo entrega el certificado. Tus datos nunca viajan por aquí.</p>
</main></body></html>"""


class _OnboardingHandler(BaseHTTPRequestHandler):
    server_version = "FAMILYFIN"
    sys_version = ""
    material: TlsMaterial
    app_url: str

    def log_message(self, fmt, *args):  # route to logging, not stderr
        logger.info("Web onboarding: %s - %s", self.client_address[0], fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        if not is_lan_address(self.client_address[0]):
            self._send(403, b"Forbidden", "text/plain")
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = _ONBOARDING_PAGE.format(url=html.escape(self.app_url),
                                           fingerprint=html.escape(self.material.ca_fingerprint))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/familyfin-ca.mobileconfig":
            self._send(200, mobileconfig(self.material), "application/x-apple-aspen-config",
                       {"Content-Disposition": 'attachment; filename="familyfin-ca.mobileconfig"'})
        elif path == "/familyfin-ca.crt":
            self._send(200, self.material.ca_cert_file.read_bytes(), "application/x-x509-ca-cert",
                       {"Content-Disposition": 'attachment; filename="familyfin-ca.crt"'})
        else:
            self._send(404, b"Not found", "text/plain")

    do_HEAD = do_GET


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class WebServer:
    def __init__(self) -> None:
        self.sessions = SessionStore()
        self.limiter = LoginLimiter()
        self.on_change: Optional[Callable[[int, str], None]] = None
        self.material: Optional[TlsMaterial] = None
        self.port: Optional[int] = None
        self.error: Optional[str] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._onboarding: Optional[ThreadingHTTPServer] = None
        self._onboarding_timer: Optional[threading.Timer] = None
        self.onboarding_until: Optional[float] = None
        self._lock = threading.RLock()

    # ── state ──────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._server is not None \
            and getattr(self._server, "started", False)

    def urls(self) -> List[str]:
        if not self.material or not self.port:
            return []
        hosts = list(self.material.addresses) + [n for n in host_names() if n.endswith(".local")]
        return [f"https://{h}:{self.port}" for h in hosts]

    def onboarding_url(self) -> Optional[str]:
        if not self._onboarding or not self.material or not self.material.addresses:
            return None
        return f"http://{self.material.addresses[0]}:{self._onboarding.server_address[1]}/"

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self, port: int = DEFAULT_PORT, host: str = "0.0.0.0", timeout: float = 10.0) -> bool:
        """Start (or restart on another port). Returns True when listening."""
        import uvicorn

        from app.web.app import create_app

        with self._lock:
            if self.running and self.port == port:
                return True
            self.stop()
            self.error = None
            try:
                self.material = ensure_tls()
            except Exception as exc:
                logger.exception("Web: no se pudieron preparar los certificados")
                self.error = f"No se pudieron crear los certificados: {exc}"
                return False

            app = create_app(sessions=self.sessions, limiter=self.limiter, on_change=self._changed)
            config = uvicorn.Config(
                app, host=host, port=port,
                ssl_certfile=str(self.material.cert_file),
                ssl_keyfile=str(self.material.key_file),
                ssl_keyfile_password=self.material.key_password,
                log_config=None, access_log=False, server_header=False, date_header=True,
                proxy_headers=False, timeout_keep_alive=10, limit_concurrency=64,
                h11_max_incomplete_event_size=64 * 1024,
            )
            config.load()
            harden_ssl_context(config.ssl)
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None   # not the main thread
            self._server = server
            self.port = port
            self._thread = threading.Thread(target=self._run, name="familyfin-web", daemon=True)
            self._thread.start()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and self._thread.is_alive() and not server.started:
                time.sleep(0.05)
            if not server.started:
                self.error = self.error or f"No se pudo abrir el puerto {port} (¿está en uso?)."
                self.stop()
                return False
            logger.info("Web: servidor HTTPS escuchando en %s", ", ".join(self.urls()))
            return True

    def _run(self) -> None:
        try:
            self._server.run()
        except BaseException as exc:  # SystemExit from uvicorn on bind errors
            self.error = f"El servidor web se detuvo: {exc}"
            logger.exception("Web: el servidor se detuvo")

    def stop(self) -> None:
        with self._lock:
            self.stop_onboarding()
            if self._server is not None:
                self._server.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout=5)
            if self._server is not None:
                imports = getattr(self._server.config.app.state, "imports", None)
                if imports is not None:
                    imports.clear()        # uploaded statements never confirmed
                logger.info("Web: servidor detenido")
            self._server = None
            self._thread = None

    def regenerate_certificates(self) -> None:
        """New CA + certificate: every device must install the profile again."""
        port = self.port or DEFAULT_PORT
        was_running = self.running
        self.stop()
        self.material = ensure_tls(regenerate=True)
        self.sessions.revoke_all()
        if was_running:
            self.start(port)

    # ── onboarding ─────────────────────────────────────────────────────
    def start_onboarding(self, seconds: int = ONBOARDING_SECONDS) -> Optional[str]:
        with self._lock:
            if not self.running or not self.material:
                self.error = "El servidor web no está activo."
                return None
            if not self.material.addresses:
                self.error = ("No se ha encontrado ninguna red local en este PC. "
                              "Conéctalo al Wi-Fi o al cable de casa y vuelve a activar el acceso web.")
                return None
            self.stop_onboarding()
            handler = type("Handler", (_OnboardingHandler,), {
                "material": self.material,
                "app_url": (self.urls() or [""])[0],
            })
            port = (self.port or DEFAULT_PORT) + ONBOARDING_PORT_OFFSET
            try:
                self._onboarding = ThreadingHTTPServer(("0.0.0.0", port), handler)
            except OSError as exc:
                self.error = f"No se pudo abrir el puerto {port} para configurar dispositivos: {exc}"
                return None
            self._onboarding.daemon_threads = True
            threading.Thread(target=self._onboarding.serve_forever, name="familyfin-onboarding",
                             daemon=True).start()
            self._onboarding_timer = threading.Timer(seconds, self.stop_onboarding)
            self._onboarding_timer.daemon = True
            self._onboarding_timer.start()
            self.onboarding_until = time.time() + seconds
            logger.info("Web: configuración de dispositivos abierta %d s en %s", seconds, self.onboarding_url())
            return self.onboarding_url()

    def stop_onboarding(self) -> None:
        with self._lock:
            if self._onboarding_timer is not None:
                self._onboarding_timer.cancel()
                self._onboarding_timer = None
            if self._onboarding is not None:
                self._onboarding.shutdown()
                self._onboarding.server_close()
                self._onboarding = None
                logger.info("Web: configuración de dispositivos cerrada")
            self.onboarding_until = None

    @property
    def onboarding_active(self) -> bool:
        return self._onboarding is not None

    # ── desktop bridge ─────────────────────────────────────────────────
    def _changed(self, user_id: int, area: str) -> None:
        callback = self.on_change
        if callback is not None:
            try:
                callback(user_id, area)
            except Exception:
                logger.exception("Web: aviso de cambios al escritorio")


_instance: Optional[WebServer] = None


def get_web_server() -> WebServer:
    global _instance
    if _instance is None:
        _instance = WebServer()
    return _instance


def start_if_enabled() -> None:
    settings = load_web_settings()
    if settings.enabled:
        server = get_web_server()
        if not server.start(settings.port):
            logger.error("Web: no arrancó: %s", server.error)


def shutdown() -> None:
    if _instance is not None:
        _instance.stop()
