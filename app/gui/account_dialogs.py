"""
app/gui/account_dialogs.py
==========================
FAMILYFIN — «Cambiar contraseña» and «Cambiar nombre» on the PC (account
menu of MainWindow), with the same rules as the web «Mi cuenta»: both run
through app.auth.account_service, and every AccountError is shown under the
field it belongs to. The dialogs only write the user row; the caller does
the rest (web sessions, audit, labels) after accept().
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)
from sqlalchemy.orm import Session

from app.auth.account_rules import MAX_PASSWORD_LENGTH, MAX_USERNAME_LENGTH, MIN_PASSWORD_LENGTH, AccountError
from app.auth.account_service import change_password, rename_user
from app.gui.investment_dialogs import dialog_stylesheet


class _AccountFormDialog(QDialog):
    """Title, hint, a column of labelled fields with an error line under
    each, a general error line and Cancelar / <action>."""

    TITLE = ""
    HINT = ""
    ACTION = ""
    # (field key as in AccountError.field, label, is a password)
    FIELDS: List[Tuple[str, str, bool]] = []

    def __init__(self, session: Session, user_id: int, theme: dict,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session = session
        self._user_id = user_id
        self.setWindowTitle(self.TITLE)
        self.setModal(True)
        self.setFixedWidth(420)
        self.setStyleSheet(dialog_stylesheet(theme))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(6)

        title = QLabel(self.TITLE)
        title.setObjectName("dlgTitle")
        layout.addWidget(title)
        hint = QLabel(self.HINT)
        hint.setObjectName("dlgHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)

        self.inputs: Dict[str, QLineEdit] = {}
        self.errors: Dict[str, QLabel] = {}
        for key, label, secret in self.FIELDS:
            layout.addWidget(QLabel(label))
            edit = QLineEdit()
            edit.setMaxLength(MAX_PASSWORD_LENGTH * 2 if secret else MAX_USERNAME_LENGTH * 2)
            if secret:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.textChanged.connect(lambda _text, k=key: self._clear_error(k))
            edit.returnPressed.connect(self._next_or_submit)
            layout.addWidget(edit)
            error = QLabel()
            error.setObjectName("dlgError")
            error.setWordWrap(True)
            error.setVisible(False)
            layout.addWidget(error)
            self.inputs[key] = edit
            self.errors[key] = error
            layout.addSpacing(4)

        self.general_error = QLabel()
        self.general_error.setObjectName("dlgError")
        self.general_error.setWordWrap(True)
        self.general_error.setVisible(False)
        layout.addWidget(self.general_error)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.setObjectName("btnSecondary")
        cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton(self.ACTION)
        self.btn_ok.setObjectName("btnPrimary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.submit)
        buttons.addWidget(cancel)
        buttons.addWidget(self.btn_ok)
        layout.addSpacing(8)
        layout.addLayout(buttons)

    def value(self, key: str) -> str:
        return self.inputs[key].text()

    def _clear_error(self, key: str) -> None:
        self.errors[key].setVisible(False)
        self.general_error.setVisible(False)

    def _next_or_submit(self) -> None:
        keys = list(self.inputs)
        for key in keys:
            if not self.inputs[key].text():
                self.inputs[key].setFocus()
                return
        self.submit()

    def show_error(self, exc: AccountError) -> None:
        label = self.errors.get(exc.field) if exc.field else None
        if label is None:
            label = self.general_error
        label.setText(exc.message)
        label.setVisible(True)
        if exc.field in self.inputs:
            edit = self.inputs[exc.field]
            if edit.echoMode() == QLineEdit.EchoMode.Password:
                edit.clear()
                label.setVisible(True)      # clear() hid it through textChanged
            edit.setFocus()

    def submit(self) -> None:
        try:
            self._session.expire_all()
            self.apply()
        except AccountError as exc:
            self.show_error(exc)
            return
        except Exception:
            self._session.rollback()
            logging.getLogger(__name__).exception("%s: error inesperado", self.TITLE)
            self.show_error(AccountError(None, "No se pudo guardar el cambio. No se ha cambiado nada."))
            return
        self.accept()

    def apply(self) -> None:          # pragma: no cover - overridden
        raise NotImplementedError


class ChangePasswordDialog(_AccountFormDialog):
    TITLE = "Cambiar contraseña"
    HINT = (f"Entre {MIN_PASSWORD_LENGTH} y {MAX_PASSWORD_LENGTH} caracteres. Mejor una frase fácil "
            "de recordar que una palabra con símbolos; no valen las muy comunes ni tu nombre. "
            "Se cerrará la sesión en la web de todos tus dispositivos.")
    ACTION = "Cambiar contraseña"
    FIELDS = [
        ("current", "Contraseña actual", True),
        ("password", "Nueva contraseña", True),
        ("confirm", "Repite la nueva contraseña", True),
    ]

    def apply(self) -> None:
        change_password(self._session, self._user_id, self.value("current"),
                        self.value("password"), self.value("confirm"))


class RenameDialog(_AccountFormDialog):
    TITLE = "Cambiar nombre de usuario"
    HINT = "Con este nombre entrarás en el PC y en la web. Tus datos no cambian."
    ACTION = "Cambiar nombre"
    FIELDS = [
        ("username", "Nuevo nombre", False),
        ("password", "Tu contraseña", True),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.names: Optional[Tuple[str, str]] = None   # (old, new) after accept()

    def apply(self) -> None:
        self.names = rename_user(self._session, self._user_id,
                                 self.value("password"), self.value("username"))
