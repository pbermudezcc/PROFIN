"""
app/gui/import_view.py
=======================
FAMILYFIN — Import View (Fase 4b)

Flow:
  Step 1 — File selection  : Browse for (or drop) one or several .xls/.xlsx
                             statements of the account
  Step 2 — Preview         : Table showing new vs duplicate rows
                             + duplicate resolution dialogs
  Step 3 — Confirm         : Commit to DB, show import summary

Design decisions:
  - Uses prepare_import() (no DB writes) for preview
  - Uses commit_import() for final DB writes
  - Preview table columns: Fecha | Emisor | Concepto | Importe | Tipo | Saldo
    (+ Extracto when several files are imported together)
  - Duplicates flagged in orange; user resolves via inline checkbox
  - Auto-categorization runs after commit
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Dict, List

from PySide6.QtCore    import Qt, QThread, Signal, QObject
from PySide6.QtGui     import QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar,
    QMessageBox, QSizePolicy, QStackedWidget,
    QAbstractItemView, QCheckBox, QScrollArea,
)
import qtawesome as qta

from app.config      import THEMES
from app.db.models   import User, TransactionType
from sqlalchemy.orm  import Session


# ---------------------------------------------------------------------------
# Background worker for parse + validate (keeps GUI responsive)
# ---------------------------------------------------------------------------

class _ParseWorker(QObject):
    finished = Signal(object)   # ImportPreview or Exception
    error    = Signal(str)

    def __init__(self, file_paths: List[Path], user_id: int, account_id: int) -> None:
        super().__init__()
        self._file_paths = file_paths
        self._user_id    = user_id
        self._account_id = account_id

    def run(self) -> None:
        # A SQLAlchemy Session is not thread-safe: this worker thread opens
        # its own short-lived session instead of using the GUI's shared one.
        from app.db.database import get_session
        from app.ingestion.importer import prepare_import

        session = get_session()
        try:
            preview = prepare_import(
                file_paths = self._file_paths,
                session    = session,
                user_id    = self._user_id,
                account_id = self._account_id,
            )
            self.finished.emit(preview)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Main Import View
# ---------------------------------------------------------------------------

class ImportView(QWidget):
    """Three-step import wizard embedded in the main stacked view."""

    STEP_SELECT  = 0
    STEP_PREVIEW = 1
    STEP_SUMMARY = 2

    COL_FILE   = 6
    COL_IMPORT = 7
    EXTENSIONS = (".xls", ".xlsx")

    def __init__(self, user: User, session: Session,
                 theme: str = "light") -> None:
        super().__init__()
        self._user     = user
        self._session  = session
        self._theme    = theme
        self._t        = THEMES[theme]
        self._preview  = None   # ImportPreview object
        self._decisions: Dict[int, bool] = {}  # row_idx -> import (True) / skip (False)
        self._account_id: Optional[int] = None

        self._build_ui()
        self._apply_theme()
        self._ensure_account()
        self.setAcceptDrops(True)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Top bar
        topbar = self._make_topbar()
        root.addWidget(topbar)

        # Step indicator
        self._step_bar = self._make_step_bar()
        root.addWidget(self._step_bar)

        # Stacked steps
        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._stack.addWidget(self._make_step_select())
        self._stack.addWidget(self._make_step_preview())
        self._stack.addWidget(self._make_step_summary())

        self._go_to_step(self.STEP_SELECT)

    def _make_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("dashTopBar")
        bar.setFixedHeight(56)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)

        lbl = QLabel("Importar movimientos")
        lbl.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        lbl.setObjectName("dashTitle")
        layout.addWidget(lbl)
        layout.addStretch()
        return bar

    def _make_step_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(48)
        bar.setObjectName("stepBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(0)

        steps = ["1  Seleccionar extractos",
                 "2  Previsualizar",
                 "3  Resultado"]
        self._step_labels: List[QLabel] = []
        for i, text in enumerate(steps):
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setObjectName(f"step_{i}")
            lbl.setFixedHeight(48)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            layout.addWidget(lbl)
            self._step_labels.append(lbl)

            if i < len(steps) - 1:
                arrow = QLabel("›")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                arrow.setFixedWidth(20)
                arrow.setObjectName("stepArrow")
                layout.addWidget(arrow)

        return bar

    def _make_step_select(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(20)
        layout.setContentsMargins(48, 48, 48, 48)

        # Drop zone
        self._drop_zone = QFrame()
        self._drop_zone.setObjectName("dropZone")
        self._drop_zone.setFixedSize(480, 220)
        dz_layout = QVBoxLayout(self._drop_zone)
        dz_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz_layout.setSpacing(12)

        lbl_icon = QLabel()
        lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_icon.setPixmap(
            qta.icon("mdi.file-excel-outline",
                     color=self._t["accent"]).pixmap(56, 56)
        )
        dz_layout.addWidget(lbl_icon)

        lbl_drop = QLabel("Selecciona o arrastra uno o varios extractos de CaixaBank")
        lbl_drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_drop.setObjectName("dropZoneText")
        dz_layout.addWidget(lbl_drop)

        lbl_hint = QLabel("Excel .xls / .xlsx — si se solapan, cada movimiento se guarda una vez")
        lbl_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_hint.setObjectName("dropZoneHint")
        dz_layout.addWidget(lbl_hint)

        dz_layout.addSpacing(8)

        self._btn_browse = QPushButton(
            "  Buscar extractos..."
        )
        self._btn_browse.setIcon(
            qta.icon("mdi.folder-open-outline", color="#FFFFFF")
        )
        self._btn_browse.setFixedHeight(40)
        self._btn_browse.setFixedWidth(200)
        self._btn_browse.setObjectName("btnPrimary")
        self._btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_browse.clicked.connect(self._on_browse)
        dz_layout.addWidget(self._btn_browse,
                            alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self._drop_zone)

        # Selected file label
        self._lbl_selected = QLabel("")
        self._lbl_selected.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_selected.setObjectName("selectedFile")
        layout.addWidget(self._lbl_selected)

        # Progress bar (hidden by default)
        self._progress = QProgressBar()
        self._progress.setFixedWidth(480)
        self._progress.setFixedHeight(6)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 0)   # indeterminate
        self._progress.setVisible(False)
        self._progress.setObjectName("importProgress")
        layout.addWidget(self._progress,
                         alignment=Qt.AlignmentFlag.AlignCenter)

        # Parse button
        self._btn_parse = QPushButton("  Analizar")
        self._btn_parse.setIcon(
            qta.icon("mdi.magnify", color="#FFFFFF")
        )
        self._btn_parse.setFixedHeight(44)
        self._btn_parse.setFixedWidth(200)
        self._btn_parse.setObjectName("btnPrimary")
        self._btn_parse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_parse.setEnabled(False)
        self._btn_parse.clicked.connect(self._on_parse)
        layout.addWidget(self._btn_parse,
                         alignment=Qt.AlignmentFlag.AlignCenter)

        self._selected_paths: List[Path] = []
        return w

    def _make_step_preview(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)

        # Summary bar
        self._preview_summary = QFrame()
        self._preview_summary.setObjectName("previewSummary")
        self._preview_summary.setFixedHeight(52)
        sum_layout = QHBoxLayout(self._preview_summary)
        sum_layout.setContentsMargins(16, 0, 16, 0)
        sum_layout.setSpacing(24)

        self._lbl_total = self._make_summary_chip(
            "mdi.database-import-outline", "0 filas", "accent"
        )
        self._lbl_new = self._make_summary_chip(
            "mdi.plus-circle-outline", "0 nuevas", "income"
        )
        self._lbl_dups = self._make_summary_chip(
            "mdi.content-copy", "0 duplicados", "warning"
        )
        self._lbl_skip = self._make_summary_chip(
            "mdi.skip-next-outline", "0 omitidas", "neutral"
        )
        for chip in [self._lbl_total, self._lbl_new,
                     self._lbl_dups, self._lbl_skip]:
            sum_layout.addWidget(chip)
        sum_layout.addStretch()

        layout.addWidget(self._preview_summary)

        # Transaction table
        self._tbl_preview = QTableWidget()
        self._tbl_preview.setColumnCount(8)
        self._tbl_preview.setHorizontalHeaderLabels([
            "Fecha", "Emisor", "Concepto",
            "Importe", "Tipo", "Saldo", "Extracto", "Importar"
        ])
        self._tbl_preview.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._tbl_preview.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._tbl_preview.setAlternatingRowColors(True)
        self._tbl_preview.verticalHeader().setVisible(False)
        self._tbl_preview.setShowGrid(False)

        hdr = self._tbl_preview.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(self.COL_FILE, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(self.COL_IMPORT, QHeaderView.ResizeMode.Fixed)
        self._tbl_preview.setColumnWidth(self.COL_IMPORT, 80)

        layout.addWidget(self._tbl_preview)

        # Bottom button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        self._btn_back_select = QPushButton("  Volver")
        self._btn_back_select.setIcon(
            qta.icon("mdi.arrow-left", color=self._t["text_secondary"])
        )
        self._btn_back_select.setFixedHeight(40)
        self._btn_back_select.setObjectName("btnSecondary")
        self._btn_back_select.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back_select.clicked.connect(
            lambda: self._go_to_step(self.STEP_SELECT)
        )

        self._btn_commit = QPushButton("  Confirmar importación")
        self._btn_commit.setIcon(
            qta.icon("mdi.database-check-outline", color="#FFFFFF")
        )
        self._btn_commit.setFixedHeight(40)
        self._btn_commit.setObjectName("btnPrimary")
        self._btn_commit.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_commit.clicked.connect(self._on_commit)

        btn_row.addWidget(self._btn_back_select)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_commit)
        layout.addLayout(btn_row)

        return w

    def _make_step_summary(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(20)
        layout.setContentsMargins(48, 48, 48, 48)

        self._lbl_summary_icon = QLabel()
        self._lbl_summary_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._lbl_summary_icon)

        self._lbl_summary_title = QLabel("")
        self._lbl_summary_title.setFont(
            QFont("Segoe UI", 18, QFont.Weight.Bold)
        )
        self._lbl_summary_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._lbl_summary_title)

        self._lbl_summary_detail = QLabel("")
        self._lbl_summary_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_summary_detail.setObjectName("summaryDetail")
        self._lbl_summary_detail.setWordWrap(True)
        layout.addWidget(self._lbl_summary_detail)

        btn_new = QPushButton("  Nueva importación")
        btn_new.setIcon(
            qta.icon("mdi.upload-outline", color="#FFFFFF")
        )
        btn_new.setFixedHeight(44)
        btn_new.setFixedWidth(220)
        btn_new.setObjectName("btnPrimary")
        btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_new.clicked.connect(
            lambda: self._go_to_step(self.STEP_SELECT)
        )
        layout.addWidget(btn_new, alignment=Qt.AlignmentFlag.AlignCenter)

        return w

    def _make_summary_chip(self, icon_name: str,
                            text: str, color_key: str) -> QLabel:
        lbl = QLabel(f"  {text}")
        lbl.setObjectName(f"chip_{color_key}")
        return lbl

    # ------------------------------------------------------------------
    # Step navigation
    # ------------------------------------------------------------------

    def _go_to_step(self, step: int) -> None:
        self._stack.setCurrentIndex(step)
        t = self._t
        for i, lbl in enumerate(self._step_labels):
            if i == step:
                lbl.setStyleSheet(
                    f"color: {t['accent']}; font-weight: 700;"
                    f"border-bottom: 2px solid {t['accent']};"
                )
            else:
                lbl.setStyleSheet(
                    f"color: {t['text_secondary']}; font-weight: 400;"
                    "border-bottom: none;"
                )

    # ------------------------------------------------------------------
    # Slots — Step 1
    # ------------------------------------------------------------------

    def _on_browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Seleccionar extractos de CaixaBank",
            os.path.expanduser("~"),
            "Ficheros CaixaBank (*.xls *.xlsx);;Todos los ficheros (*)"
        )
        if paths:
            self._select([Path(p) for p in paths])

    def _select(self, paths: List[Path]) -> None:
        self._selected_paths = paths
        if len(paths) == 1:
            self._lbl_selected.setText(f"📄  {paths[0].name}")
        else:
            self._lbl_selected.setText(f"📄  {len(paths)} extractos seleccionados")
        self._lbl_selected.setToolTip("\n".join(p.name for p in paths))
        self._btn_parse.setEnabled(True)

    def _dropped_statements(self, event) -> List[Path]:
        if not event.mimeData().hasUrls():
            return []
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        return [p for p in paths if p.suffix.lower() in self.EXTENSIONS]

    def dragEnterEvent(self, event) -> None:
        if self._stack.currentIndex() == self.STEP_SELECT and self._dropped_statements(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = self._dropped_statements(event)
        if paths and self._btn_browse.isEnabled():
            self._select(paths)
            event.acceptProposedAction()

    def _on_parse(self) -> None:
        if not self._selected_paths:
            return
        if self._account_id is None:
            QMessageBox.warning(
                self, "Sin cuenta",
                "No se encontró ninguna cuenta bancaria. "
                "Por favor crea una cuenta antes de importar."
            )
            return

        self._btn_parse.setEnabled(False)
        self._btn_browse.setEnabled(False)
        self._progress.setVisible(True)

        # Run in background thread
        self._thread = QThread()
        self._worker = _ParseWorker(
            list(self._selected_paths), self._user.id, self._account_id
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_parse_done)
        self._worker.error.connect(self._on_parse_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_parse_done(self, preview) -> None:
        self._progress.setVisible(False)
        self._btn_parse.setEnabled(True)
        self._btn_browse.setEnabled(True)
        self._preview = preview
        self._populate_preview_table(preview)
        self._go_to_step(self.STEP_PREVIEW)
        if preview.rejected:
            QMessageBox.warning(
                self, "Extractos no incluidos",
                "Estos ficheros no se importarán:\n\n" + "\n".join(
                    f"• {r.name}: {r.reason}" for r in preview.rejected
                ),
            )

    def _on_parse_error(self, error_msg: str) -> None:
        self._progress.setVisible(False)
        self._btn_parse.setEnabled(True)
        self._btn_browse.setEnabled(True)
        QMessageBox.critical(
            self, "Error de validación", error_msg
        )

    # ------------------------------------------------------------------
    # Preview table population
    # ------------------------------------------------------------------

    def _populate_preview_table(self, preview) -> None:
        """
        Populate the preview table from an ImportPreview object.

        ImportPreview structure (real):
          preview.df                  → pd.DataFrame (all rows)
          preview.duplicate_result    → DuplicateCheckResult
            .clean        → List[int]  row indices safe to import
            .auto_skip    → List[int]  certain duplicates (auto-skipped)
            .needs_review → List[int]  possible duplicates (user decides)
            .review_reasons → dict[int, str]
        """
        t          = self._t
        df         = preview.df
        dup_result = preview.duplicate_result

        n_total    = len(df)
        n_skip     = len(dup_result.auto_skip)
        n_review   = len(dup_result.needs_review)
        n_clean    = len(dup_result.clean)
        many       = len(preview.files) > 1

        self._lbl_total.setText(
            f"  {n_total} filas en {len(preview.files)} extractos" if many
            else f"  {n_total} filas"
        )
        self._lbl_new.setText(f"  {n_clean} nuevas")
        self._lbl_dups.setText(f"  {n_review} posibles duplicados")
        self._lbl_skip.setText(f"  {n_skip} omitidas (exactas)")

        # Only show clean + needs_review rows (auto_skip are hidden)
        visible_indices = sorted(dup_result.clean + dup_result.needs_review)

        self._tbl_preview.setRowCount(len(visible_indices))
        self._tbl_preview.setColumnHidden(self.COL_FILE, not many)
        self._decisions = {}

        type_labels = {
            TransactionType.income:     "Ingreso",
            TransactionType.expense:    "Gasto",
            TransactionType.investment: "Inversión",
        }

        for tbl_row, df_idx in enumerate(visible_indices):
            row       = df.iloc[df_idx]
            is_review = df_idx in dup_result.needs_review

            # -- Fecha
            d = row.get("transaction_date", "")
            item_date = QTableWidgetItem(str(d) if d else "")

            # -- Emisor
            item_issuer = QTableWidgetItem(
                str(row.get("issuer_name", "") or "")[:40]
            )

            # -- Concepto
            item_concept = QTableWidgetItem(
                str(row.get("concept", "") or "")[:80]
            )

            # -- Importe
            amount = float(row.get("amount", 0.0) or 0.0)
            item_amount = QTableWidgetItem(self._fmt_amount(amount))
            item_amount.setTextAlignment(
                Qt.AlignmentFlag.AlignRight |
                Qt.AlignmentFlag.AlignVCenter
            )
            item_amount.setForeground(
                QColor(t["income"] if amount >= 0 else t["expense"])
            )

            # -- Tipo (derived from amount sign)
            if amount > 0:
                txn_type = TransactionType.income
            elif amount < 0:
                txn_type = TransactionType.expense
            else:
                txn_type = None
            item_type = QTableWidgetItem(
                type_labels.get(txn_type, "—")
            )

            # -- Saldo
            balance = float(row.get("running_balance", 0.0) or 0.0)
            item_balance = QTableWidgetItem(self._fmt_amount(balance))
            item_balance.setTextAlignment(
                Qt.AlignmentFlag.AlignRight |
                Qt.AlignmentFlag.AlignVCenter
            )

            # -- Extracto (source file of the row)
            item_file = QTableWidgetItem(str(row.get("source_file", "") or ""))

            # -- Apply row styling
            for col, item in enumerate([
                item_date, item_issuer, item_concept,
                item_amount, item_type, item_balance, item_file
            ]):
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled |
                    Qt.ItemFlag.ItemIsSelectable
                )
                if is_review:
                    # Orange tint for possible duplicates
                    item.setBackground(QColor(t["warning"] + "30"))
                    item.setToolTip(
                        dup_result.review_reasons.get(df_idx, "Posible duplicado")
                    )
                self._tbl_preview.setItem(tbl_row, col, item)

            # -- Checkbox column: clean=checked, needs_review=unchecked
            chk = QCheckBox()
            chk.setChecked(not is_review)
            chk.setStyleSheet("margin-left: 24px;")
            # Store decision keyed by df_idx (not tbl_row) for commit_import
            self._decisions[df_idx] = not is_review
            chk.stateChanged.connect(
                lambda state, idx=df_idx: self._decisions.__setitem__(
                    idx, state == Qt.CheckState.Checked.value
                )
            )
            self._tbl_preview.setCellWidget(tbl_row, self.COL_IMPORT, chk)

        self._tbl_preview.resizeRowsToContents()
        import logging
        logging.getLogger(__name__).info(
            "Preview: %d filas visibles (%d limpias, %d revisión, %d omitidas)",
            len(visible_indices), n_clean, n_review, n_skip,
        )

    # ------------------------------------------------------------------
    # Slots — Step 2 (Commit)
    # ------------------------------------------------------------------

    def _on_commit(self) -> None:
        if self._preview is None:
            return

        to_import = sum(1 for v in self._decisions.values() if v)
        if to_import == 0:
            QMessageBox.information(
                self, "Sin filas seleccionadas",
                "No hay filas marcadas para importar. "
                "Selecciona al menos una fila."
            )
            return

        reply = QMessageBox.question(
            self,
            "Confirmar importación",
            f"Se importarán <b>{to_import}</b> transacciones "
            f"a la base de datos.<br><br>¿Continuar?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            from app.ingestion.import_service import run_import

            outcome = run_import(
                self._session, self._user.id, self._preview,
                self._account_id, self._decisions,
            )
            result   = outcome.summary
            auto_cat = outcome.auto_categorized

            self._show_summary(
                success     = True,
                imported    = result.rows_imported,
                skipped     = result.rows_skipped,
                auto_cat    = auto_cat,
                files       = len(result.batch_ids),
            )

        except Exception as exc:
            self._show_summary(success=False, error=str(exc))

    def _show_summary(self, success: bool,
                       imported: int = 0, skipped: int = 0,
                       auto_cat: int = 0, files: int = 1,
                       error: str = "") -> None:
        t = self._t
        if success:
            self._lbl_summary_icon.setPixmap(
                qta.icon("mdi.check-circle-outline",
                         color=t["income"]).pixmap(72, 72)
            )
            self._lbl_summary_title.setText("¡Importación completada!")
            self._lbl_summary_title.setStyleSheet(
                f"color: {t['income']}; font-size: 20px; font-weight: 700;"
            )
            self._lbl_summary_detail.setText(
                (f"<b>{files}</b> extractos  ·  " if files > 1 else "") +
                f"<b>{imported}</b> transacciones importadas  ·  "
                f"<b>{skipped}</b> omitidas  ·  "
                f"<b>{auto_cat}</b> auto-categorizadas"
            )
        else:
            self._lbl_summary_icon.setPixmap(
                qta.icon("mdi.alert-circle-outline",
                         color=t["expense"]).pixmap(72, 72)
            )
            self._lbl_summary_title.setText("Error en la importación")
            self._lbl_summary_title.setStyleSheet(
                f"color: {t['expense']}; font-size: 20px; font-weight: 700;"
            )
            self._lbl_summary_detail.setText(error)

        self._go_to_step(self.STEP_SUMMARY)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_account(self) -> None:
        """Get or create default account for this user."""
        from app.ingestion.import_service import ensure_default_account
        self._account_id = ensure_default_account(self._session, self._user.id).id

    @staticmethod
    def _fmt_amount(value: float) -> str:
        sign = "+" if value >= 0 else ""
        return (
            f"{sign}{abs(value):,.2f} €"
            .replace(",", "X").replace(".", ",").replace("X", ".")
        )

    def _apply_theme(self) -> None:
        t = self._t
        self.setStyleSheet(f"""
            QFrame#dashTopBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QLabel#dashTitle {{
                color: {t["text_primary"]};
                font-size: 16px; font-weight: 700;
            }}
            QFrame#stepBar {{
                background-color: {t["bg_secondary"]};
                border-bottom: 1px solid {t["border"]};
            }}
            QFrame#dropZone {{
                background-color: {t["bg_secondary"]};
                border: 2px dashed {t["border_strong"]};
                border-radius: 16px;
            }}
            QLabel#dropZoneText {{
                color: {t["text_primary"]};
                font-size: 14px; font-weight: 600;
            }}
            QLabel#dropZoneHint {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QLabel#selectedFile {{
                color: {t["accent"]};
                font-size: 13px; font-weight: 600;
            }}
            QPushButton#btnPrimary {{
                background-color: {t["accent"]};
                color: #FFFFFF; border: none;
                border-radius: 8px;
                font-size: 13px; font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton#btnPrimary:hover {{
                background-color: {t["accent_hover"]};
            }}
            QPushButton#btnPrimary:disabled {{
                background-color: {t["border"]};
                color: {t["text_secondary"]};
            }}
            QPushButton#btnSecondary {{
                background-color: transparent;
                color: {t["text_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                font-size: 13px;
                padding: 0 16px;
            }}
            QPushButton#btnSecondary:hover {{
                background-color: {t["bg_hover"]};
            }}
            QTableWidget {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                gridline-color: transparent;
                color: {t["text_primary"]};
                font-size: 12px;
            }}
            QTableWidget::item {{
                padding: 6px 8px;
                border-bottom: 1px solid {t["border"]};
            }}
            QTableWidget::item:selected {{
                background-color: {t["accent_light"]};
                color: {t["accent"]};
            }}
            QHeaderView::section {{
                background-color: {t["bg_primary"]};
                color: {t["text_secondary"]};
                font-size: 11px; font-weight: 700;
                padding: 6px 8px;
                border: none;
                border-bottom: 2px solid {t["border"]};
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            QTableWidget::item:alternate {{
                background-color: {t["bg_primary"]};
            }}
            QFrame#previewSummary {{
                background-color: {t["bg_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
            }}
            QProgressBar#importProgress {{
                background-color: {t["border"]};
                border-radius: 3px;
            }}
            QProgressBar#importProgress::chunk {{
                background-color: {t["accent"]};
                border-radius: 3px;
            }}
            QLabel#summaryDetail {{
                color: {t["text_secondary"]};
                font-size: 14px;
            }}
        """)