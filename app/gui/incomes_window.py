"""Income analysis view.

The income screen deliberately shares the complete period/filter/chart
implementation with the expense screen so both financial views stay aligned.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import TransactionType
from app.gui.expenses_view import ExpensesView


class IncomesWindow(ExpensesView):
    """Income analysis using the expense view's period-aware presentation."""

    TRANSACTION_TYPE = TransactionType.income
    VIEW_TITLE = "Ingresos"
    TOTAL_LABEL = "Total ingresos"
    MAX_LABEL = "Mayor ingreso"
    TABLE_OBJECT_NAME = "incomeTable"
    AMOUNT_COLOR_KEY = "income"
    AMOUNT_COLOR_FALLBACK = "#16A34A"
    LINE_TITLE = "Evolución diaria de ingresos"
    LOG_NAME = "IncomesWindow"
    DETAIL_HEADER = "Detalle de ingresos"

    def __init__(
        self,
        user,
        session: Session,
        theme: str = "light",
        parent=None,
    ) -> None:
        super().__init__(session, user, theme)
        if parent is not None:
            self.setParent(parent)

    def load_entries(self) -> None:
        """Compatibility alias for callers of the previous income view."""
        self._load_data()
