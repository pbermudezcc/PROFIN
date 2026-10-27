"""
tools/demo/capture_screens.py
=============================
FAMILYFIN — Screenshots of the desktop app and the web front-end on the DEMO
data, for the user manual (docs/manual/img/).

    python tools/demo/capture_screens.py            # web client (what the manual uses)
    python tools/demo/capture_screens.py --desktop  # the desktop app too

Desktop: the real windows are built with Qt.WA_DontShowOnScreen (laid out
and painted by the native Windows platform, never shown) and grabbed. Web: the FastAPI app is served over HTTPS on 127.0.0.1 (throwaway
self-signed certificate) with a session pre-opened for DEMO, and Microsoft
Edge (headless) takes the pictures at iPhone and laptop widths, in light or
dark theme. The pending October statement is uploaded for real so the
import preview (with its duplicates) can be captured. Run build_demo.py first.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_SCALE_FACTOR", "1")

import _sandbox  # noqa: F401  (must run before any app import)
from _sandbox import DATA_DIR, DEMO_USER, ROOT, STATEMENTS_DIR, assert_isolated

OUT = ROOT / "docs" / "manual" / "img"
SIZE = (1600, 940)


# ---------------------------------------------------------------------------
# Desktop
# ---------------------------------------------------------------------------
def capture_desktop() -> None:
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication, QTabWidget

    from app.db import database

    database.init_db()
    assert_isolated()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")

    from app.db.models import User
    from app.gui.login_window import LoginWindow
    from app.gui.main_window import MainWindow

    MainWindow.showMaximized = lambda self: None     # never on the real screen

    def settle(ms: int = 900) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def shot(widget, name: str) -> None:
        settle()
        path = OUT / f"{name}.png"
        widget.grab().save(str(path))
        print(f"  {path.relative_to(ROOT)}")

    for theme in ("light", "dark"):
        login = LoginWindow()
        login.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        login._theme = theme
        login._apply_theme()
        login.show()
        if hasattr(login, "_inp_user"):
            login._inp_user.setText(DEMO_USER)
        shot(login, f"desktop_login_{theme}")
        login.close()

    session = database.SessionLocal()
    user = session.query(User).filter(User.username == DEMO_USER).one()

    for theme in ("light", "dark"):
        win = MainWindow(user=user, session=session, theme=theme)
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        win.resize(*SIZE)
        win.show()
        suffix = "" if theme == "light" else "_dark"
        views = ["dashboard", "expenses", "income", "investments", "transactions",
                 "categories", "rules", "import", "settings"] if theme == "light" else \
                ["dashboard", "expenses", "investments", "transactions"]
        for key in views:
            win._navigate(key)
            view = win._views[key]
            if key == "investments":
                tabs = view.findChild(QTabWidget)
                for i, label in enumerate(["posiciones", "operaciones", "evolucion", "distribucion", "fiscal"]):
                    if theme == "dark" and label not in ("posiciones", "evolucion"):
                        continue
                    tabs.setCurrentIndex(i)
                    if label == "fiscal":       # 2025 has the Santander sale (gains + dividends)
                        year = tabs.currentWidget()._cmb_year
                        year.setCurrentIndex(max(0, year.findText("2025")))
                    settle(1500)
                    shot(win, f"desktop_investments_{label}{suffix}")
                continue
            if key == "categories":
                view._tree.expandAll()
            combo = getattr(view, "_cmb_period", None) or getattr(view, "_cmb_range", None)
            if combo is not None:       # a full year of data reads better than half a month
                combo.setCurrentIndex(combo.findText("Año actual"))
            shot(win, f"desktop_{key}{suffix}")
        win.close()
        settle(300)
    session.close()


# ---------------------------------------------------------------------------
# Web (headless Edge)
# ---------------------------------------------------------------------------
EDGE = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
]
# (file name, path, width, height, theme, css selector to click once loaded)
# w_* = laptop/iPad landscape width (sidebar layout), m_* = iPhone width.
WIDE, PHONE = 1366, 390
WEB_PAGES = [
    ("w_login", "/login", WIDE, 820, "light", None),
    ("w_login_dark", "/login", WIDE, 820, "dark", None),
    ("w_resumen", "/?rango=year", WIDE, 1180, "light", None),
    ("w_resumen_dark", "/?rango=year", WIDE, 1180, "dark", None),
    ("w_movimientos", "/movimientos", WIDE, 900, "light", None),
    ("w_movimiento_ficha", "/movimientos", WIDE, 900, "light", "#tx-{uncategorized}"),
    ("w_gastos", "/gastos?periodo=anio", WIDE, 1500, "light", None),
    ("w_ingresos", "/ingresos?periodo=anio", WIDE, 1100, "light", None),
    ("w_importar", "/importar", WIDE, 820, "light", None),
    ("w_importar_preview", "{preview}", WIDE, 1150, "light", None),
    ("w_importaciones", "/mas/importaciones", WIDE, 900, "light", None),
    ("w_categorias", "/mas/categorias", WIDE, 1000, "light", None),
    ("w_reglas", "/mas/reglas", WIDE, 1000, "light", None),
    ("w_regla_nueva", "/mas/reglas", WIDE, 900, "light", '[hx-get="/mas/reglas/nueva"]'),
    ("w_inv_posiciones", "/inversiones", WIDE, 1000, "light", None),
    ("w_inv_operaciones", "/inversiones/operaciones", WIDE, 1000, "light", None),
    ("w_inv_evolucion", "/inversiones/evolucion", WIDE, 1000, "light", None),
    ("w_inv_distribucion", "/inversiones/distribucion", WIDE, 1000, "light", None),
    ("w_inv_fiscal", "/inversiones/fiscal?anio=2025", WIDE, 1100, "light", None),
    ("w_inv_evolucion_dark", "/inversiones/evolucion", WIDE, 1000, "dark", None),
    ("w_mas", "/mas", WIDE, 900, "light", None),
    ("w_cuentas", "/mas/cuentas", WIDE, 820, "light", None),
    ("m_login", "/login", PHONE, 844, "light", None),
    ("m_resumen", "/?rango=year", PHONE, 1500, "light", None),
    ("m_resumen_dark", "/?rango=year", PHONE, 1500, "dark", None),
    ("m_movimientos", "/movimientos", PHONE, 1300, "light", None),
    ("m_movimiento_ficha", "/movimientos", PHONE, 844, "light", "#tx-{uncategorized}"),
    ("m_gastos", "/gastos?periodo=anio", PHONE, 1500, "light", None),
    ("m_inversiones", "/inversiones", PHONE, 1400, "light", None),
    ("m_inv_evolucion", "/inversiones/evolucion", PHONE, 1100, "dark", None),
    ("m_importar_preview", "{preview}", PHONE, 1300, "light", None),
    ("m_mas", "/mas", PHONE, 1000, "light", None),
    ("m_reglas", "/mas/reglas", PHONE, 1100, "light", None),
]
SCALE = {WIDE: 1.5, PHONE: 2}

# Wrapper page: sets the theme (same origin, so the framed page's theme.js
# reads it), frames the page at an exact width and optionally clicks once.
_FRAME = """<!doctype html><body style="margin:0;background:#888">
<script>try {{ localStorage.setItem("ff-theme", "{theme}"); }} catch (e) {{}}</script>
<iframe id="f" src="{src}" style="width:{w}px;height:{h}px;border:0;display:block"></iframe>
<script>
var f = document.getElementById("f"), sel = {click};
f.addEventListener("load", function () {{
  if (!sel) return;
  setTimeout(function () {{ var el = f.contentDocument.querySelector(sel); if (el) el.click(); }}, 700);
}});
</script></body>"""


def _self_signed(folder: Path) -> tuple[Path, Path]:
    """A throwaway certificate for 127.0.0.1, so the capture runs over HTTPS."""
    import datetime as dt
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FAMILYFIN demo capture")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def capture_web(port: int = 8799) -> None:
    import json
    from urllib.parse import quote

    import requests
    import urllib3
    import uvicorn
    from PySide6.QtGui import QGuiApplication, QImage
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import HTMLResponse

    from app.db import database
    from app.db.models import Transaction, User
    from app.web.app import create_app
    from app.web.security import CSRF_FIELD, SESSION_COOKIE, LoginLimiter, SessionStore

    database.init_db()
    assert_isolated()
    browser = next((p for p in EDGE if p.exists()), None)
    if browser is None:
        print("  No se encontró Edge/Chrome: se omiten las capturas web.")
        return

    with database.SessionLocal() as s:
        user_id = s.query(User.id).filter(User.username == DEMO_USER).scalar()
        uncategorized = (s.query(Transaction.id)
                         .filter(Transaction.user_id == user_id, Transaction.category_id.is_(None))
                         .order_by(Transaction.transaction_date.desc()).limit(1).scalar())
    sessions = SessionStore()
    web_session = sessions.create(user_id, DEMO_USER, "127.0.0.1", "manual-capture")

    app = create_app(sessions=sessions, limiter=LoginLimiter(), on_change=lambda *a: None, lan_only=True)

    class _CaptureHelpers(BaseHTTPMiddleware):
        """Capture-only: DEMO's session on every page but /login, and the frame wrapper."""

        async def dispatch(self, request, call_next):
            if request.url.path == "/__frame":
                q = request.query_params
                click = json.dumps(q.get("click") or None)
                return HTMLResponse(_FRAME.format(theme=q["theme"], src=q["src"], w=q["w"], h=q["h"], click=click))
            if request.url.path != "/login":
                headers = [(k, v) for k, v in request.scope["headers"] if k != b"cookie"]
                headers.append((b"cookie", f"{SESSION_COOKIE}={web_session.sid}".encode()))
                request.scope["headers"] = headers
            response = await call_next(request)
            for header in ("content-security-policy", "x-frame-options"):
                if header in response.headers:
                    del response.headers[header]
            return response

    app.add_middleware(_CaptureHelpers)
    tls_dir = DATA_DIR.parent / "capture-tls"
    cert, key = _self_signed(tls_dir)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           ssl_certfile=str(cert), ssl_keyfile=str(key)))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.1)
    base = f"https://127.0.0.1:{port}"

    # A real upload of the pending October statement, for the preview pages.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    pending = sorted(STATEMENTS_DIR.glob("*PENDIENTE*.xlsx"))
    preview = "/importar"
    if pending:
        with pending[0].open("rb") as fh:
            r = requests.post(f"{base}/importar", verify=False, allow_redirects=False, headers={"Origin": base},
                              files={"fichero": (pending[0].name, fh)}, data={CSRF_FIELD: web_session.csrf})
        if r.status_code == 303:
            preview = r.headers["location"]
            print(f"  Extracto pendiente subido para la previsualización ({preview})")
        else:
            print(f"  AVISO: la subida del extracto devolvió {r.status_code}; sin previsualización")

    profile = DATA_DIR.parent / "edge-capture-profile"
    try:
        qt = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841  (QImage needs it)
        for name, path, w, h, theme, click in WEB_PAGES:
            out = OUT / f"{name}.png"
            out.unlink(missing_ok=True)
            scale = SCALE[w]
            path = path.format(preview=preview)
            click = click.format(uncategorized=uncategorized) if click else ""
            # Headless Edge will not make a window narrower than ~500 px, so
            # the page is framed at its exact width and the picture cropped.
            url = (f"{base}/__frame?w={w}&h={h}&theme={theme}&click={quote(click, safe='')}"
                   f"&src={quote(path, safe='')}")
            subprocess.run([
                str(browser), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                "--ignore-certificate-errors", "--lang=es-ES", "--accept-lang=es-ES,es", f"--user-data-dir={profile}",
                f"--force-device-scale-factor={scale}", f"--window-size={max(w, 800)},{h}",
                "--virtual-time-budget=9000", f"--screenshot={out}", url,
            ], check=False, capture_output=True, timeout=90)
            if out.exists():
                QImage(str(out)).copy(0, 0, round(w * scale), round(h * scale)).save(str(out))
                print(f"  {out.relative_to(ROOT)}")
            else:
                print(f"  FALLO {name}")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(tls_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--web", action="store_true")
    args = parser.parse_args()
    if not (DATA_DIR / "profin.db").exists():
        sys.exit("Primero: python tools/demo/build_demo.py")
    OUT.mkdir(parents=True, exist_ok=True)
    if args.desktop:
        print("Capturas de escritorio:")
        capture_desktop()
    if args.web or not args.desktop:
        print("Capturas web:")
        capture_web()


if __name__ == "__main__":
    main()
