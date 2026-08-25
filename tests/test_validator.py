"""
tests/test_validator.py
========================
Unit tests for import validation (Phase 2).

Covers all V-01 to V-08 rules.
"""
from __future__ import annotations

import pytest
from datetime import date

import pandas as pd

from app.ingestion.validator import (
    ValidationError,
    ValidationWarning,
    validate_dataframe,
)


def _base_df(n: int = 3) -> pd.DataFrame:
    """Return a minimal valid DataFrame for validation tests."""
    return pd.DataFrame({
        "transaction_date": [date(2025, 1, i + 1) for i in range(n)],
        "value_date":       [date(2025, 1, i + 1) for i in range(n)],
        "amount":           [-50.0 * (i + 1) for i in range(n)],
        "running_balance":  [1000.0 - 50.0 * (i + 1) for i in range(n)],
        "concepto_comun":   ["044"] * n,
        "concepto_propio":  ["000"] * n,
        "issuer_name":      ["MERCADONA"] * n,
        "concept":          ["MERCADONA | TCR"] * n,
        "raw_col14":        ["MERCADONA"] * n,
        "currency":         ["EUR"] * n,
        "iban_raw":         ["2100 0732 21 0102117363"] * n,
    })


class TestValidateDataframe:

    def test_valid_df_passes(self):
        result = validate_dataframe(_base_df())
        assert result.row_count == 3
        assert result.iban == "2100 0732 21 0102117363"
        assert result.warnings == []

    def test_v01_empty_df(self):
        with pytest.raises(ValidationError, match="no contiene filas"):
            validate_dataframe(pd.DataFrame())

    def test_v02_missing_column(self):
        df = _base_df()
        df = df.drop(columns=["amount"])
        with pytest.raises(ValidationError, match="'amount'"):
            validate_dataframe(df)

    def test_v03_null_date(self):
        df = _base_df()
        df.loc[1, "transaction_date"] = None
        with pytest.raises(ValidationError, match="Fecha operación"):
            validate_dataframe(df)

    def test_v04_null_amount(self):
        df = _base_df()
        df.loc[0, "amount"] = None
        with pytest.raises(ValidationError, match="Importe"):
            validate_dataframe(df)

    def test_v05_null_balance(self):
        df = _base_df()
        df.loc[2, "running_balance"] = None
        with pytest.raises(ValidationError, match="Saldo"):
            validate_dataframe(df)

    def test_v06_non_eur_currency(self):
        df = _base_df()
        df.loc[0, "currency"] = "USD"
        with pytest.raises(ValidationError, match="EUR"):
            validate_dataframe(df)

    def test_v07_multiple_ibans(self):
        df = _base_df()
        df.loc[0, "iban_raw"] = "2100 0001 00 0000000000"
        with pytest.raises(ValidationError, match="IBANs distintos"):
            validate_dataframe(df)

    def test_v07_no_iban(self):
        df = _base_df()
        df["iban_raw"] = None
        with pytest.raises(ValidationError, match="IBAN"):
            validate_dataframe(df)

    def test_v08_zero_amount_warning(self):
        df = _base_df()
        df.loc[1, "amount"] = 0.0
        result = validate_dataframe(df)
        assert len(result.warnings) == 1
        assert result.warnings[0].rule == "V-08"
        assert 1 in result.warnings[0].row_indices