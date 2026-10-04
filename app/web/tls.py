"""
app/web/tls.py
==============
FAMILYFIN — Private certificate authority and server certificate for the
LAN web server.

A browser only trusts HTTPS on a LAN address if it trusts the issuer, so
the app creates its own small CA once ("FAMILYFIN Home CA") and signs a
server certificate for the PC's current LAN addresses with it. Each
iPhone/iPad installs and trusts the CA certificate once (see onboarding in
app/web/bootstrap.py); after that Safari shows a normal padlock, and later
server certificates (new IP, renewal) need no action on the devices.

Files live in %LOCALAPPDATA%/FAMILYFIN/web_tls, never in the data folder:
the certificates belong to this PC (its addresses), and the data folder is
what gets backed up, zipped and possibly synced elsewhere. Private keys are
PEM files encrypted with a random passphrase; the passphrase is stored
protected by Windows DPAPI (only this Windows user on this PC can read it).
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import ipaddress
import logging
import os
import secrets
import socket
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

CA_VALIDITY = timedelta(days=3650)
# Apple caps TLS server certificates at 825 days for private CAs and 398
# for public ones; 397 keeps every client happy.
SERVER_VALIDITY = timedelta(days=397)
RENEW_BEFORE = timedelta(days=30)

CA_CERT = "ca.crt"
CA_KEY = "ca.key"
SERVER_CERT = "server.crt"
SERVER_KEY = "server.key"
SECRET = "keys.secret"


def tls_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return (root / "FAMILYFIN" / "web_tls").resolve()


# ---------------------------------------------------------------------------
# DPAPI (Windows) — protects the key passphrase at rest
# ---------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not func(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise OSError("DPAPI no pudo proteger/leer la clave de los certificados.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _write_secret(path: Path, passphrase: bytes) -> None:
    if sys.platform == "win32":
        path.write_bytes(b"DPAPI1" + _dpapi(passphrase, protect=True))
    else:  # development on other platforms: owner-only file
        path.write_bytes(b"PLAIN1" + passphrase)
        path.chmod(0o600)


def _read_secret(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(b"DPAPI1"):
        return _dpapi(raw[6:], protect=False)
    if raw.startswith(b"PLAIN1"):
        return raw[6:]
    raise ValueError("Fichero de clave de certificados no reconocido.")


# ---------------------------------------------------------------------------
# LAN addresses
# ---------------------------------------------------------------------------

def is_lan_address(address: str) -> bool:
    """Private (RFC 1918 / ULA) or loopback; never link-local or public."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    return ip.is_private and not ip.is_link_local and not ip.is_unspecified


def lan_ipv4_addresses() -> List[str]:
    """This PC's private IPv4 addresses, the default-route one first."""
    found: List[str] = []
    try:
        # No packet is sent: connect() on UDP only selects the outgoing interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    result: List[str] = []
    for address in found:
        ip = ipaddress.ip_address(address)
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and address not in result:
            result.append(address)
    return result


def host_names() -> List[str]:
    name = socket.gethostname().strip().lower()
    return [name, f"{name}.local"] if name else []


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

@dataclass
class TlsMaterial:
    cert_file: Path
    key_file: Path
    key_password: str
    ca_cert_file: Path
    ca_fingerprint: str      # SHA-256 of the CA certificate, AA:BB:… form
    server_expires: datetime
    addresses: List[str]


def _fingerprint(cert: x509.Certificate) -> str:
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _key_pem(key, passphrase: bytes) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )


def _create_ca(folder: Path, passphrase: bytes) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "PC"
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FAMILYFIN"),
        x509.NameAttribute(NameOID.COMMON_NAME, f"FAMILYFIN Home CA ({host})"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    (folder / CA_KEY).write_bytes(_key_pem(key, passphrase))
    (folder / CA_CERT).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Web: autoridad de certificación creada (%s)", _fingerprint(cert))


def _issue_server_cert(folder: Path, passphrase: bytes, addresses: List[str], names: List[str]) -> None:
    ca_cert = x509.load_pem_x509_certificate((folder / CA_CERT).read_bytes())
    ca_key = serialization.load_pem_private_key((folder / CA_KEY).read_bytes(), passphrase)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    san: List[x509.GeneralName] = [x509.DNSName("localhost")]
    san += [x509.DNSName(n) for n in names]
    san += [x509.IPAddress(ipaddress.ip_address(a)) for a in ["127.0.0.1", *addresses]]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FAMILYFIN"),
            x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "familyfin"),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + SERVER_VALIDITY)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=False, crl_sign=False,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    (folder / SERVER_KEY).write_bytes(_key_pem(key, passphrase))
    (folder / SERVER_CERT).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Web: certificado del servidor emitido para %s", ", ".join(addresses) or "localhost")


def _server_cert_ok(folder: Path, addresses: List[str], names: List[str]) -> bool:
    path = folder / SERVER_CERT
    if not path.exists() or not (folder / SERVER_KEY).exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        ca_cert = x509.load_pem_x509_certificate((folder / CA_CERT).read_bytes())
        cert.verify_directly_issued_by(ca_cert)
    except Exception:
        return False
    if cert.not_valid_after_utc - datetime.now(timezone.utc) < RENEW_BEFORE:
        return False
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    dns = set(san.get_values_for_type(x509.DNSName))
    return set(addresses) <= ips and set(names) <= dns


def ensure_tls(
    folder: Optional[Path] = None,
    addresses: Optional[List[str]] = None,
    regenerate: bool = False,
) -> TlsMaterial:
    """Create the CA once and (re)issue the server certificate when needed.

    regenerate=True replaces the CA too: every device must then install the
    new CA certificate again (used when the user asks to "Regenerar").
    """
    folder = folder or tls_dir()
    folder.mkdir(parents=True, exist_ok=True)
    addresses = lan_ipv4_addresses() if addresses is None else addresses
    names = host_names()

    secret_path = folder / SECRET
    if regenerate:
        for name in (CA_CERT, CA_KEY, SERVER_CERT, SERVER_KEY, SECRET):
            (folder / name).unlink(missing_ok=True)
    if not secret_path.exists() or not (folder / CA_CERT).exists() or not (folder / CA_KEY).exists():
        for name in (CA_CERT, CA_KEY, SERVER_CERT, SERVER_KEY):
            (folder / name).unlink(missing_ok=True)
        _write_secret(secret_path, secrets.token_urlsafe(32).encode())
        _create_ca(folder, _read_secret(secret_path))
    passphrase = _read_secret(secret_path)
    if not _server_cert_ok(folder, addresses, names):
        _issue_server_cert(folder, passphrase, addresses, names)

    ca_cert = x509.load_pem_x509_certificate((folder / CA_CERT).read_bytes())
    server_cert = x509.load_pem_x509_certificate((folder / SERVER_CERT).read_bytes())
    return TlsMaterial(
        cert_file=folder / SERVER_CERT,
        key_file=folder / SERVER_KEY,
        key_password=passphrase.decode(),
        ca_cert_file=folder / CA_CERT,
        ca_fingerprint=_fingerprint(ca_cert),
        server_expires=server_cert.not_valid_after_utc,
        addresses=addresses,
    )


def ca_der(material: TlsMaterial) -> bytes:
    cert = x509.load_pem_x509_certificate(material.ca_cert_file.read_bytes())
    return cert.public_bytes(serialization.Encoding.DER)


def mobileconfig(material: TlsMaterial) -> bytes:
    """iOS configuration profile that installs the CA as a root certificate.

    After installing it the user still has to switch it on in Ajustes →
    General → Información → Ajustes de confianza de certificados (iOS never
    trusts a root certificate for websites automatically).
    """
    der = ca_der(material)
    digest = hashlib.sha256(der).hexdigest()
    payload_uuid = str(uuid.UUID(digest[:32]))
    profile_uuid = str(uuid.UUID(digest[32:64]))
    data = base64.encodebytes(der).decode()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>PayloadCertificateFileName</key><string>familyfin-ca.cer</string>
      <key>PayloadContent</key><data>
{data}</data>
      <key>PayloadDescription</key><string>Certificado raíz de FAMILYFIN para la conexión segura en casa.</string>
      <key>PayloadDisplayName</key><string>FAMILYFIN Home CA</string>
      <key>PayloadIdentifier</key><string>es.familyfin.ca.{payload_uuid}</string>
      <key>PayloadType</key><string>com.apple.security.root</string>
      <key>PayloadUUID</key><string>{payload_uuid}</string>
      <key>PayloadVersion</key><integer>1</integer>
    </dict>
  </array>
  <key>PayloadDescription</key><string>Permite abrir FAMILYFIN desde este dispositivo con conexión cifrada (HTTPS).</string>
  <key>PayloadDisplayName</key><string>FAMILYFIN — conexión segura</string>
  <key>PayloadIdentifier</key><string>es.familyfin.profile.{profile_uuid}</string>
  <key>PayloadRemovalDisallowed</key><false/>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadUUID</key><string>{profile_uuid}</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict>
</plist>
""".encode("utf-8")
