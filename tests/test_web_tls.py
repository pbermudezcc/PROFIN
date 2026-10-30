"""
tests/test_web_tls.py
=====================
Certificates and the real HTTPS server: CA chain, SANs, renewal, TLS
version floor, and the onboarding listener that only hands out the CA.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509

from app.web import server as web_server
from app.web import tls


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_port_with_onboarding() -> int:
    """A free port whose onboarding neighbour (bound on 0.0.0.0) is free too.

    Taken below Windows' ephemeral range (49152+): free_port() returns an
    ephemeral port N, and Windows hands out the next ones roughly in order,
    so a client connection made by the test itself often took N + 1 — the
    onboarding port — and the onboarding tests failed now and then."""
    import random

    for _ in range(100):
        port = random.randrange(20000, 40000)
        try:
            with socket.socket() as a, socket.socket() as b:
                a.bind(("127.0.0.1", port))
                b.bind(("0.0.0.0", port + web_server.ONBOARDING_PORT_OFFSET))
        except OSError:
            continue
        return port
    pytest.skip("no free pair of ports for the onboarding server")


def test_server_certificate_chains_to_ca_and_covers_lan_addresses(tmp_path):
    material = tls.ensure_tls(tmp_path, addresses=["192.168.1.20"])
    ca = x509.load_pem_x509_certificate(material.ca_cert_file.read_bytes())
    cert = x509.load_pem_x509_certificate(material.cert_file.read_bytes())
    cert.verify_directly_issued_by(ca)

    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("192.168.1.20") in san.get_values_for_type(x509.IPAddress)
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    validity = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert validity <= timedelta(days=398)          # iOS limit

    # Keys are encrypted at rest; the passphrase is not stored in clear.
    assert b"ENCRYPTED PRIVATE KEY" in material.key_file.read_bytes()
    assert material.key_password.encode() not in (tmp_path / tls.SECRET).read_bytes()


def test_certificate_reused_then_reissued_for_new_address(tmp_path):
    first = tls.ensure_tls(tmp_path, addresses=["192.168.1.20"])
    pem = first.cert_file.read_bytes()
    assert tls.ensure_tls(tmp_path, addresses=["192.168.1.20"]).cert_file.read_bytes() == pem
    second = tls.ensure_tls(tmp_path, addresses=["192.168.1.20", "10.0.0.7"])
    assert second.cert_file.read_bytes() != pem
    assert second.ca_fingerprint == first.ca_fingerprint       # devices keep trusting it
    third = tls.ensure_tls(tmp_path, addresses=["10.0.0.7"], regenerate=True)
    assert third.ca_fingerprint != first.ca_fingerprint


def test_lan_address_filter():
    for ok in ("192.168.1.2", "10.1.2.3", "172.16.0.9", "127.0.0.1", "::1", "fd00::1"):
        assert tls.is_lan_address(ok), ok
    for bad in ("8.8.8.8", "169.254.1.1", "fe80::1", "0.0.0.0", "testclient", ""):
        assert not tls.is_lan_address(bad), bad


def test_mobileconfig_contains_only_the_ca(tmp_path):
    material = tls.ensure_tls(tmp_path, addresses=["192.168.1.20"])
    profile = tls.mobileconfig(material).decode()
    assert "com.apple.security.root" in profile
    assert "PRIVATE KEY" not in profile


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    material = tls.ensure_tls(tmp_path, addresses=[])
    monkeypatch.setattr(web_server, "ensure_tls", lambda *a, **k: material)
    srv = web_server.WebServer()
    port = free_port_with_onboarding()
    assert srv.start(port, host="127.0.0.1"), srv.error
    yield srv, port, material
    srv.stop()


def _client_context(material) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(material.ca_cert_file))
    ctx.check_hostname = True
    return ctx


def test_https_server_serves_login_with_trusted_ca(running_server):
    srv, port, material = running_server
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=_client_context(material), timeout=10)
    conn.request("GET", "/login")
    response = conn.getresponse()
    body = response.read().decode()
    assert response.status == 200 and "FAMILYFIN" in body
    assert response.getheader("strict-transport-security")
    version = conn.sock.version()
    conn.close()
    assert version in ("TLSv1.3", "TLSv1.2")
    assert srv.running


def test_plain_http_is_not_served(running_server):
    _, port, _ = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/login")
        body = conn.getresponse().read()
    except (OSError, http.client.HTTPException):
        return                                  # connection refused/reset: fine
    finally:
        conn.close()
    assert b"FAMILYFIN" not in body


def test_tls_below_1_2_is_refused(running_server):
    _, port, material = running_server
    ctx = _client_context(material)
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_1
        ctx.maximum_version = ssl.TLSVersion.TLSv1_1
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except (ValueError, ssl.SSLError):
        pytest.skip("this OpenSSL build cannot even offer TLS 1.1")
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost"):
                pass


def test_onboarding_listener_serves_only_the_ca(running_server, monkeypatch):
    srv, port, material = running_server
    material.addresses = ["127.0.0.1"]
    url = srv.start_onboarding(seconds=30)
    assert url and srv.onboarding_active, srv.error
    onboarding_port = port + web_server.ONBOARDING_PORT_OFFSET
    try:
        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", onboarding_port, timeout=5)
            conn.request("GET", path)
            r = conn.getresponse()
            data = r.read()
            conn.close()
            return r.status, data

        status, page = get("/")
        assert status == 200 and material.ca_fingerprint.encode() in page
        status, profile = get("/familyfin-ca.mobileconfig")
        assert status == 200 and b"com.apple.security.root" in profile
        for path in ("/login", "/api/resumen", "/movimientos", "/static/css/app.css"):
            assert get(path)[0] == 404, path
    finally:
        srv.stop_onboarding()
    assert not srv.onboarding_active
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", onboarding_port), timeout=2).close()


def test_onboarding_closes_by_itself(running_server):
    import time
    srv, port, material = running_server
    material.addresses = ["127.0.0.1"]
    assert srv.start_onboarding(seconds=1), srv.error
    deadline = time.time() + 5
    while srv.onboarding_active and time.time() < deadline:
        time.sleep(0.1)
    assert not srv.onboarding_active


def test_server_reports_busy_port(tmp_path, monkeypatch):
    material = tls.ensure_tls(tmp_path, addresses=[])
    monkeypatch.setattr(web_server, "ensure_tls", lambda *a, **k: material)
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        srv = web_server.WebServer()
        assert not srv.start(port, host="127.0.0.1", timeout=5)
        assert srv.error
        srv.stop()


def test_server_cert_expiry_triggers_renewal(tmp_path, monkeypatch):
    tls.ensure_tls(tmp_path, addresses=["192.168.1.20"])
    pem = (tmp_path / tls.SERVER_CERT).read_bytes()
    future = datetime.now(timezone.utc) + tls.SERVER_VALIDITY - timedelta(days=10)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(tls, "datetime", _Clock)
    tls.ensure_tls(tmp_path, addresses=["192.168.1.20"])
    assert (tmp_path / tls.SERVER_CERT).read_bytes() != pem


def test_onboarding_explains_missing_lan(running_server):
    srv, _, material = running_server
    material.addresses = []
    assert srv.start_onboarding(seconds=5) is None
    assert "red local" in srv.error


def test_configure_button_is_usable_while_server_is_off(monkeypatch):
    """The button used to be disabled while the server was off, which looked
    like 'nothing happens'. Now it offers to start the server."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.gui import web_access_view

    app = QApplication.instance() or QApplication([])  # noqa: F841
    page = web_access_view.WebAccessPage()
    assert page._btn_onboard.isEnabled()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No)
    page._btn_onboard.click()
    assert asked and "apagado" in asked[0]
    assert not page._server.running
