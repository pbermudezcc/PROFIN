"""
app/gui/web_access_view.py
==========================
FAMILYFIN — Configuración → "Acceso web": turn the LAN HTTPS server on/off,
show the address (and its QR), set up iPhones/iPads (trust the home CA),
manage certificates and open web sessions.

Changes apply immediately (they do not wait for "Guardar").
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.web.server import WebSettings, get_web_server, load_web_settings, save_web_settings

logger = logging.getLogger(__name__)


def qr_pixmap(text: str, size: int = 168) -> QPixmap:
    """Draw a QR code with QPainter (no image library needed)."""
    import qrcode

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor("white"))
    painter = QPainter(pixmap)
    cell = size / len(matrix)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.drawRect(QRectF(x * cell, y * cell, cell + 0.5, cell + 0.5))
    painter.end()
    return pixmap


class WebAccessPage(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._server = get_web_server()
        self._settings = load_web_settings()
        self._qr_for: Optional[str] = None
        self._onb_qr_for: Optional[str] = None
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    # ── UI ─────────────────────────────────────────────────────────────
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        group = QGroupBox("Acceso desde la red de casa")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._enabled_cb = QCheckBox("Permitir abrir FAMILYFIN desde iPhone, iPad u otros equipos (HTTPS)")
        self._enabled_cb.setChecked(self._settings.enabled)
        self._enabled_cb.toggled.connect(self._on_toggle)
        form.addRow("Servidor", self._enabled_cb)

        port_row = QHBoxLayout()
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65000)
        self._port_spin.setValue(self._settings.port)
        port_row.addWidget(self._port_spin)
        self._btn_port = QPushButton("Aplicar puerto")
        self._btn_port.clicked.connect(self._apply_port)
        port_row.addWidget(self._btn_port)
        port_row.addStretch()
        form.addRow("Puerto", port_row)

        self._status = QLabel()
        self._status.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("Estado", self._status)

        address_row = QHBoxLayout()
        self._urls = QLabel()
        self._urls.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._urls.setWordWrap(True)
        address_row.addWidget(self._urls, 1)
        self._qr = QLabel()
        self._qr.setFixedSize(168, 168)
        address_row.addWidget(self._qr)
        form.addRow("Dirección", address_row)

        note = QLabel(
            "Solo responde a equipos de tu red local y siempre con conexión cifrada. "
            "La primera vez Windows preguntará por el firewall: permite el acceso solo en "
            "<b>redes privadas</b>. El servidor funciona mientras FAMILYFIN esté abierto."
        )
        note.setWordWrap(True)
        note.setObjectName("webNote")
        form.addRow("", note)
        layout.addWidget(group)

        devices = QGroupBox("Configurar un iPhone o iPad (una vez por dispositivo)")
        dev_layout = QVBoxLayout(devices)
        steps = QLabel(
            "1. Pulsa «Configurar dispositivo» y escanea el código con la cámara del iPhone/iPad.<br>"
            "2. Descarga el perfil e instálalo en Ajustes → Perfil descargado.<br>"
            "3. Actívalo en Ajustes → General → Información → Ajustes de confianza de certificados.<br>"
            "4. Abre la dirección de arriba y añádela a la pantalla de inicio."
        )
        steps.setTextFormat(Qt.TextFormat.RichText)
        steps.setWordWrap(True)
        dev_layout.addWidget(steps)

        onb_row = QHBoxLayout()
        self._btn_onboard = QPushButton("Configurar dispositivo")
        self._btn_onboard.setObjectName("btnPrimary")
        self._btn_onboard.setFixedHeight(34)
        self._btn_onboard.clicked.connect(self._toggle_onboarding)
        onb_row.addWidget(self._btn_onboard)
        self._onb_label = QLabel()
        self._onb_label.setWordWrap(True)
        self._onb_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        onb_row.addWidget(self._onb_label, 1)
        self._onb_qr = QLabel()
        self._onb_qr.setFixedSize(168, 168)
        onb_row.addWidget(self._onb_qr)
        dev_layout.addLayout(onb_row)

        self._fingerprint = QLabel()
        self._fingerprint.setWordWrap(True)
        self._fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dev_layout.addWidget(self._fingerprint)

        cert_row = QHBoxLayout()
        self._cert_label = QLabel()
        cert_row.addWidget(self._cert_label, 1)
        btn_regen = QPushButton("Regenerar certificados")
        btn_regen.clicked.connect(self._regenerate)
        cert_row.addWidget(btn_regen)
        dev_layout.addLayout(cert_row)
        layout.addWidget(devices)

        sessions = QGroupBox("Sesiones web abiertas")
        ses_layout = QVBoxLayout(sessions)
        self._sessions = QListWidget()
        self._sessions.setMaximumHeight(120)
        ses_layout.addWidget(self._sessions)
        btn_revoke = QPushButton("Cerrar todas las sesiones web")
        btn_revoke.clicked.connect(self._revoke_all)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn_revoke)
        ses_layout.addLayout(row)
        layout.addWidget(sessions)
        layout.addStretch()

    # ── behaviour ──────────────────────────────────────────────────────
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def _start(self) -> None:
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            ok = self._server.start(self._settings.port)
        finally:
            self.unsetCursor()
        if not ok:
            QMessageBox.warning(self, "Acceso web", self._server.error or "No se pudo iniciar el servidor web.")

    def _on_toggle(self, enabled: bool) -> None:
        self._settings = WebSettings(enabled=enabled, port=self._port_spin.value())
        save_web_settings(self._settings)
        if enabled:
            self._start()
        else:
            self._server.stop()
        self.refresh()

    def _apply_port(self) -> None:
        self._settings = WebSettings(enabled=self._enabled_cb.isChecked(), port=self._port_spin.value())
        save_web_settings(self._settings)
        if self._settings.enabled:
            self._start()
        self.refresh()

    def _toggle_onboarding(self) -> None:
        if self._server.onboarding_active:
            self._server.stop_onboarding()
        else:
            if not self._server.running:
                answer = QMessageBox.question(
                    self, "Acceso web",
                    "El acceso web está apagado. ¿Activarlo ahora para configurar el dispositivo?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                self._enabled_cb.setChecked(True)      # starts the server (_on_toggle)
                if not self._server.running:
                    return                              # _start already showed the error
            if not self._server.start_onboarding():
                QMessageBox.warning(self, "Acceso web",
                                    self._server.error or "No se pudo abrir la configuración de dispositivos.")
        self.refresh()

    def _regenerate(self) -> None:
        answer = QMessageBox.question(
            self, "Regenerar certificados",
            "Se creará un certificado de casa nuevo. Todos los iPhone/iPad tendrán que instalar "
            "el perfil otra vez y se cerrarán las sesiones web abiertas.\n\n¿Continuar?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._server.regenerate_certificates()
        except Exception as exc:
            logger.exception("Web: regenerar certificados")
            QMessageBox.critical(self, "Regenerar certificados", str(exc))
        self._qr_for = None
        self.refresh()

    def _revoke_all(self) -> None:
        count = self._server.sessions.revoke_all()
        QMessageBox.information(self, "Sesiones web", f"Se han cerrado {count} sesiones.")
        self.refresh()

    def refresh(self) -> None:
        server = self._server
        running = server.running
        self._port_spin.setEnabled(True)
        self._btn_port.setEnabled(self._enabled_cb.isChecked())
        if running:
            self._status.setText('<span style="color:#16A34A">●</span> Activo · HTTPS')
        elif self._enabled_cb.isChecked():
            self._status.setText(f'<span style="color:#DC2626">●</span> Detenido · {server.error or "error"}')
        else:
            self._status.setText('<span style="color:#94A3B8">●</span> Desactivado')

        urls = server.urls() if running else []
        if urls:
            self._urls.setText("<br>".join(f"<b>{u}</b>" for u in urls))
            if self._qr_for != urls[0]:
                self._qr.setPixmap(qr_pixmap(urls[0]))
                self._qr_for = urls[0]
        else:
            self._urls.setText("—" if not running else "No se ha encontrado ninguna red local.")
            self._qr.clear()
            self._qr_for = None

        material = server.material
        if material:
            self._fingerprint.setText(f"Huella del certificado de casa (SHA-256):\n{material.ca_fingerprint}")
            self._cert_label.setText(
                f"Certificado del servidor válido hasta el {material.server_expires:%d/%m/%Y} "
                "(se renueva solo)."
            )
        else:
            self._fingerprint.setText("")
            self._cert_label.setText("Los certificados se crean al activar el servidor.")

        onb_url = server.onboarding_url() if server.onboarding_active else None
        if onb_url:
            remaining = max(0, int((server.onboarding_until or 0) - time.time()))
            self._btn_onboard.setText("Cerrar configuración")
            self._onb_label.setText(
                f"Escanea el código o abre <b>{onb_url}</b> en Safari.<br>"
                f"Se cierra sola en {remaining // 60}:{remaining % 60:02d}."
            )
            if self._onb_qr_for != onb_url:
                self._onb_qr.setPixmap(qr_pixmap(onb_url))
                self._onb_qr_for = onb_url
        else:
            self._btn_onboard.setText("Configurar dispositivo")
            self._onb_label.setText("")
            self._onb_qr.clear()
            self._onb_qr_for = None

        self._qr.setVisible(self._qr_for is not None)
        self._onb_qr.setVisible(self._onb_qr_for is not None)

        self._sessions.clear()
        for s in server.sessions.active():
            device = "iPad" if "iPad" in s.user_agent else ("iPhone" if "iPhone" in s.user_agent else "Navegador")
            idle = int(time.time() - s.last_seen) // 60
            self._sessions.addItem(f"{s.username} · {device} · {s.ip} · activo hace {idle} min")
        if not self._sessions.count():
            self._sessions.addItem("Ninguna")
