"""
app/gui/library_dialog.py
=========================
FAMILYFIN — "Importar categorías y reglas" preview dialog (CategoriesView).

Shows what a .profin-cats.json file would change for the logged-in user,
lets them choose Fusionar / Reemplazar (and, on the PC only, whether the
file's global rules are imported), and recomputes the preview on every
change. Nothing is written here: the caller runs library_io.apply_import
after accept().
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit,
    QRadioButton, QVBoxLayout, QHBoxLayout, QWidget,
)
from sqlalchemy.orm import Session

from app.categorization import library_io as lio
from app.categorization.rules_engine import shares_global_rules


class LibraryImportDialog(QDialog):

    def __init__(self, session: Session, user_id: int, lib: lio.LibraryFile,
                 file_name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session = session
        self._user_id = user_id
        self._lib = lib
        self.plan: Optional[lio.ImportPlan] = None

        self.setWindowTitle("Importar categorías y reglas")
        self.setModal(True)
        self.resize(640, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title = QLabel("Importar categorías y reglas")
        title.setObjectName("dlgTitle")
        layout.addWidget(title)

        origin = [file_name]
        if lib.source_user:
            origin.append(f"exportado por {lib.source_user}")
        if lib.exported_at:
            origin.append(lib.exported_at.replace("T", " ").rstrip("Z"))
        info = QLabel(" · ".join(origin) + f"\n{lib.category_count} categorías y "
                      f"{len(lib.rules)} reglas en el archivo.")
        info.setObjectName("dlgHint")
        info.setWordWrap(True)
        layout.addWidget(info)

        modes = QHBoxLayout()
        self._merge = QRadioButton("Fusionar (solo añade lo que falta)")
        self._merge.setChecked(True)
        self._replace = QRadioButton("Reemplazar mis reglas por las del archivo")
        for radio in (self._merge, self._replace):
            radio.toggled.connect(self._refresh)
            modes.addWidget(radio)
        layout.addLayout(modes)

        self._globals = QCheckBox("Importar también las reglas globales (para toda la familia)")
        self._globals.setVisible(any(r.scope == lio.SCOPE_GLOBAL for r in lib.rules)
                                 and shares_global_rules(session, user_id))
        self._globals.toggled.connect(self._refresh)
        layout.addWidget(self._globals)

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        layout.addWidget(self._details, 1)

        note = QLabel("Las categorías nunca se borran y los movimientos no se tocan. "
                      "Antes de importar se guarda una copia de tus categorías y reglas actuales.")
        note.setObjectName("dlgHint")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._ok = self._buttons.addButton("Importar", QDialogButtonBox.ButtonRole.AcceptRole)
        self._ok.setObjectName("btnPrimary")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._refresh()

    @property
    def mode(self) -> str:
        return lio.MODE_REPLACE if self._replace.isChecked() else lio.MODE_MERGE

    @property
    def allow_global(self) -> bool:
        return self._globals.isVisible() and self._globals.isChecked()

    def _refresh(self, *_args) -> None:
        plan = lio.plan_import(self._session, self._user_id, self._lib, self.mode, self.allow_global)
        self.plan = plan
        parts = [
            f"<b>{len(plan.new_types) + len(plan.new_categories)}</b> categorías nuevas",
            f"<b>{len(plan.new_rules)}</b> reglas nuevas",
            f"<b>{plan.unchanged_rules}</b> ya existentes",
        ]
        if plan.mode == lio.MODE_REPLACE:
            parts.append(f"<b>{len(plan.priority_changes)}</b> con prioridad cambiada")
            parts.append(f"<b style='color:#C0392B'>{len(plan.deleted_rules)}</b> reglas eliminadas")
        self._summary.setText(" · ".join(parts))

        lines = [f"⚠ {w}" for w in plan.warnings]
        if lines:
            lines.append("")
        for title, items in (
            ("Tipos nuevos", plan.new_types),
            ("Categorías nuevas", plan.new_categories),
            ("Reglas nuevas", [f"«{r.pattern}» ({r.match_type}, prioridad {r.priority}) → "
                               f"{r.type} › {r.category}"
                               + (" [global]" if r.scope == lio.SCOPE_GLOBAL else "")
                               for r in plan.new_rules]),
            ("Prioridad cambiada", [f"«{r.pattern}»: {old} → {r.priority}"
                                    for r, old in plan.priority_changes]),
            ("Reglas que se eliminan", plan.deleted_rules),
        ):
            if items:
                lines.append(f"{title} ({len(items)}):")
                lines.extend(f"  • {item}" for item in items)
                lines.append("")
        if not plan.has_changes:
            lines.append("No hay nada que importar: ya tienes todo lo que contiene el archivo.")
        self._details.setPlainText("\n".join(lines).rstrip())
        self._ok.setEnabled(plan.has_changes)
