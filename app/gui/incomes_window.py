from __future__ import annotations

from datetime import date
import logging

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QDateEdit, QLineEdit,
    QDoubleSpinBox, QComboBox, QPushButton, QTableView, QHeaderView, QApplication, QMessageBox
)
from PySide6.QtGui import QStandardItemModel, QStandardItem
from PySide6.QtCore import Qt, QDate

from sqlalchemy.orm import Session

from app.config import THEMES
from app.db.models import (
    Transaction, TransactionType, Account, Category
)


class IncomesWindow(QWidget):
    """Ventana de Ingresos con persistencia mínima.

    Constructor:
      IncomesWindow(user, session: Session, theme: str = "light", parent=None)

    Funcionalidad añadida:
      - Persiste ingresos como Transaction(type=TransactionType.income)
      - Crea una cuenta por defecto si no existe (como ImportView)
      - Carga ingresos guardados para el usuario
      - Categoría intenta mapear contra Category.name (opcional)

    Esta es una implementación básica: validación y mejoras posteriores se
    realizarán según feedback (paginación, filtros, edición, gráficos...).
    """

    def __init__(self, user, session: Session, theme: str = "light", parent=None):
        super().__init__(parent)
        self._user = user
        self._session = session
        self._theme = theme
        self._t = THEMES.get(theme, THEMES["light"]) if hasattr(THEMES, 'get') else THEMES[theme]

        self.setWindowTitle("Ingresos — PROFIN")
        self.resize(900, 600)

        self.logger = logging.getLogger(__name__)

        main_layout = QVBoxLayout(self)

        # --- Formulario de alta ---------------------------------
        form_layout = QHBoxLayout()

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate())

        self.amount_edit = QDoubleSpinBox()
        self.amount_edit.setMaximum(10_000_000.00)
        self.amount_edit.setPrefix("€ ")
        self.amount_edit.setDecimals(2)

        self.category_combo = QComboBox()
        # Se rellenará desde BD si hay categorías; cae a ejemplos si no
        self.category_combo.addItems(["Salario", "Freelance", "Intereses", "Otros"])

        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("Descripción (opcional)")

        self.add_btn = QPushButton("Agregar ingreso")
        self.add_btn.clicked.connect(self.add_entry)

        form_layout.addWidget(QLabel("Fecha:"))
        form_layout.addWidget(self.date_edit)
        form_layout.addWidget(QLabel("Importe:"))
        form_layout.addWidget(self.amount_edit)
        form_layout.addWidget(QLabel("Categoría:"))
        form_layout.addWidget(self.category_combo)
        form_layout.addWidget(self.desc_edit)
        form_layout.addWidget(self.add_btn)

        main_layout.addLayout(form_layout)

        # --- Tabla de entradas ----------------------------------
        self.table = QTableView()
        self.model = QStandardItemModel(0, 4, self)
        self.model.setHorizontalHeaderLabels(["Fecha", "Importe", "Categoría", "Descripción"])
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(self.table.SelectRows)
        self.table.setEditTriggers(self.table.NoEditTriggers)

        main_layout.addWidget(self.table)

        # --- Resumen simple ------------------------------------
        summary_layout = QHBoxLayout()
        self.total_label = QLabel("Total: € 0.00")
        summary_layout.addStretch()
        summary_layout.addWidget(self.total_label)
        main_layout.addLayout(summary_layout)

        # Prepare DB-backed state
        self._account_id: int | None = None
        self._ensure_account()
        self._load_categories()
        self.load_entries()

    # ----------------- Persistence helpers -------------------
    def _ensure_account(self) -> None:
        """Get or create default account for this user, like ImportView."""
        account = (
            self._session.query(Account)
            .filter(Account.user_id == self._user.id)
            .first()
        )
        if account:
            self._account_id = account.id
        else:
            acct = Account(
                user_id=self._user.id,
                account_name="Cuenta principal",
                iban_last4="0000",
            )
            self._session.add(acct)
            self._session.commit()
            self._session.refresh(acct)
            self._account_id = acct.id

    def _load_categories(self) -> None:
        try:
            cats = (
                self._session.query(Category)
                .filter(Category.user_id == self._user.id)
                .order_by(Category.name)
                .all()
            )
            if cats:
                self.category_combo.clear()
                for c in cats:
                    self.category_combo.addItem(c.name)
        except Exception:
            # Silently fallback to defaults if category table isn't ready
            self.logger.debug("No se pudieron cargar categorías desde la BD.")

    def load_entries(self) -> None:
        """Carga los últimos ingresos del usuario desde la BD."""
        self.model.removeRows(0, self.model.rowCount())
        try:
            rows = (
                self._session.query(Transaction)
                .filter(Transaction.user_id == self._user.id)
                .filter(Transaction.type == TransactionType.income)
                .order_by(Transaction.transaction_date.desc())
                .limit(500)
                .all()
            )
            for txn in rows:
                cat_name = txn.category.name if getattr(txn, 'category', None) else "-"
                self._append_row(
                    QDate(txn.transaction_date.year, txn.transaction_date.month, txn.transaction_date.day),
                    float(txn.amount),
                    cat_name,
                    txn.concept or "",
                )
        except Exception as exc:
            self.logger.exception("Error cargando ingresos: %s", exc)
        self._update_summary()

    def _append_row(self, date: QDate, amount: float, category: str, description: str) -> None:
        row = [
            QStandardItem(date.toString("yyyy-MM-dd")),
            QStandardItem(f"€ {amount:,.2f}"),
            QStandardItem(category),
            QStandardItem(description or ""),
        ]
        for item in row:
            item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.model.appendRow(row)

    def add_entry(self) -> None:
        """Persistir un nuevo ingreso en la BD y añadirlo a la tabla."""
        qdate = self.date_edit.date()
        try:
            txn_date: date = qdate.toPython()
        except Exception:
            # Fallback for older Qt versions
            txn_date = date(qdate.year(), qdate.month(), qdate.day())

        amount = float(self.amount_edit.value())
        category = self.category_combo.currentText()
        desc = self.desc_edit.text().strip()

        if amount <= 0:
            QMessageBox.warning(self, "Importe inválido", "El importe debe ser mayor que 0.")
            return

        # Map category name to Category id if exists
        category_id = None
        try:
            cat = (
                self._session.query(Category)
                .filter(Category.user_id == self._user.id)
                .filter(Category.name == category)
                .first()
            )
            if cat:
                category_id = cat.id
        except Exception:
            self.logger.debug("No se pudo mapear categoría %s", category)

        # Crear Transaction
        try:
            txn = Transaction(
                user_id=self._user.id,
                account_id=self._account_id,
                transaction_date=txn_date,
                amount=amount,
                type=TransactionType.income,
                category_id=category_id,
                concept=desc,
            )
            self._session.add(txn)
            self._session.commit()
            self._session.refresh(txn)

            # Añadir a la vista
            self._append_row(
                QDate(txn.transaction_date.year, txn.transaction_date.month, txn.transaction_date.day),
                float(txn.amount),
                category if category else "-",
                txn.concept or "",
            )
            self._update_summary()

            # Limpiar campos
            self.desc_edit.clear()
            self.amount_edit.setValue(0.0)

            self.logger.info("Ingreso persistido id=%s amount=%s", txn.id, txn.amount)

        except Exception as exc:
            self._session.rollback()
            self.logger.exception("Error guardando ingreso: %s", exc)
            QMessageBox.critical(self, "Error", f"No se pudo guardar el ingreso: {exc}")

    def _update_summary(self) -> None:
        total = 0.0
        for row in range(self.model.rowCount()):
            text = self.model.item(row, 1).text()
            try:
                val = float(text.replace('€', '').replace(',', '').strip())
            except Exception:
                val = 0.0
            total += val
        self.total_label.setText(f"Total: € {total:,.2f}")


if __name__ == "__main__":
    import sys
    from app.db.database import get_session
    from app.db.models import User

    sess = get_session()
    # Simple demo: use first user found or create a temporary one
    user = sess.query(User).first()
    if user is None:
        from app.auth.auth_service import create_user
        create_user(sess, "demo", "password")
        user = sess.query(User).first()

    app = QApplication(sys.argv)
    w = IncomesWindow(user, sess)
    w.show()
    sys.exit(app.exec())
