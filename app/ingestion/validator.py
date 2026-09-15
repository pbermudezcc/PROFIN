"""
app/ingestion/validator.py
===========================
FAMILYFIN — Import Validation Layer

Validation rules (in order):
  V-01  DataFrame is not empty.
  V-02  All required output columns are present.
  V-03  transaction_date: no null values.
  V-04  amount: no null values.
  V-05  running_balance: no null values.
  V-06  currency: all rows are 'EUR'.
  V-07  iban_raw: all rows share the same IBAN.
  V-08  amount != 0: zero-amount rows flagged as warnings (not errors).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pandas as pd


# ---------------------------------------------------------------------------
# Custom exceptions / result types
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Blocking validation failure. Message shown directly in GUI."""


@dataclass
class ValidationWarning:
    """Non-blocking warning surfaced to the user during import preview."""
    rule:        str
    message:     str
    row_indices: List[int] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Returned by validate_dataframe() on success."""
    row_count: int
    iban:      str
    warnings:  List[ValidationWarning] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Required columns
# ---------------------------------------------------------------------------
_REQUIRED_COLUMNS = [
    "transaction_date",
    "value_date",
    "amount",
    "running_balance",
    "concepto_comun",
    "concepto_propio",
    "issuer_name",
    "concept",
    "raw_col14",
    "currency",
    "iban_raw",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_dataframe(df: pd.DataFrame) -> ValidationResult:
    """
    Run all validation rules on a parsed DataFrame.

    Returns
    -------
    ValidationResult on success.

    Raises
    ------
    ValidationError on any blocking failure (V-01 to V-07).
    """
    warnings: List[ValidationWarning] = []

    # V-01 — Not empty
    if df.empty:
        raise ValidationError(
            "El fichero no contiene filas de datos. "
            "Comprueba que el export de CaixaBank no esté vacío."
        )

    # V-02 — Required columns present
    missing_cols = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValidationError(
            "Columnas requeridas no encontradas tras el parseo: "
            + ", ".join(f"'{c}'" for c in missing_cols)
            + ". Puede indicar un cambio en el formato de export de CaixaBank."
        )

    # V-03 — transaction_date: no nulls
    null_dates = df["transaction_date"].isna()
    if null_dates.any():
        bad_rows = df.index[null_dates].tolist()
        raise ValidationError(
            f"'Fecha operación': {len(bad_rows)} fila(s) sin fecha válida "
            f"(filas: {bad_rows[:5]}{'...' if len(bad_rows) > 5 else ''}). "
            "Comprueba que todas las filas tengan fecha en formato dd/mm/yyyy."
        )

    # V-04 — amount: no nulls
    null_amounts = df["amount"].isna()
    if null_amounts.any():
        bad_rows = df.index[null_amounts].tolist()
        raise ValidationError(
            f"'Importe': {len(bad_rows)} fila(s) sin importe válido "
            f"(filas: {bad_rows[:5]}{'...' if len(bad_rows) > 5 else ''}). "
            "Cada fila debe tener un valor en Ingreso o Gasto."
        )

    # V-05 — running_balance: no nulls
    null_balance = df["running_balance"].isna()
    if null_balance.any():
        bad_rows = df.index[null_balance].tolist()
        raise ValidationError(
            f"'Saldo': {len(bad_rows)} fila(s) sin saldo válido "
            f"(filas: {bad_rows[:5]}{'...' if len(bad_rows) > 5 else ''}). "
            "Todas las filas deben tener un saldo numérico."
        )

    # V-06 — currency: EUR only
    non_eur = df[df["currency"] != "EUR"]
    if not non_eur.empty:
        bad_currencies = non_eur["currency"].unique().tolist()
        raise ValidationError(
            "FAMILYFIN v1 soporta únicamente la divisa EUR. "
            f"Se encontraron: {bad_currencies} en {len(non_eur)} fila(s)."
        )

    # V-07 — iban_raw: single IBAN per file
    unique_ibans = df["iban_raw"].dropna().unique()
    if len(unique_ibans) == 0:
        raise ValidationError(
            "No se encontró ningún IBAN en el fichero. "
            "Comprueba que el export sea de CaixaBank."
        )
    if len(unique_ibans) > 1:
        raise ValidationError(
            f"El fichero contiene {len(unique_ibans)} IBANs distintos: "
            f"{unique_ibans.tolist()}. "
            "FAMILYFIN espera un fichero de una única cuenta."
        )
    detected_iban = str(unique_ibans[0]).strip()

    # V-08 — Zero amounts: warning only
    zero_amounts = df[df["amount"] == 0.0]
    if not zero_amounts.empty:
        warnings.append(ValidationWarning(
            rule="V-08",
            message=(
                f"{len(zero_amounts)} fila(s) con importe = 0,00 €. "
                "Se importarán pero pueden indicar ajustes contables."
            ),
            row_indices=zero_amounts.index.tolist(),
        ))

    return ValidationResult(
        row_count=len(df),
        iban=detected_iban,
        warnings=warnings,
    )