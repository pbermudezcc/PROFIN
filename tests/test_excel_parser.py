from pathlib import Path

import pandas as pd

from app.ingestion.excel_parser import parse_excel


def test_parse_excel_removes_repeated_caixabank_headers(monkeypatch, tmp_path):
    header = [None] * 25
    header[4] = "F. Operación"
    header[5] = "F. Valor"

    first_transaction = [None] * 25
    first_transaction[1] = "2100 0000 00 0000000000"
    first_transaction[3] = "EUR"
    first_transaction[4] = "12/11/2021"
    first_transaction[5] = "10/11/2021"
    first_transaction[7] = 0.7
    first_transaction[8] = 100.0

    second_transaction = first_transaction.copy()
    second_transaction[4] = "11/11/2021"
    second_transaction[5] = "09/11/2021"

    raw = pd.DataFrame([
        [None] * 25,
        header,
        first_transaction,
        header,
        second_transaction,
    ])
    monkeypatch.setattr(pd, "read_excel", lambda *args, **kwargs: raw)

    source = tmp_path / "movimientos.xls"
    source.touch()
    result = parse_excel(Path(source))

    assert len(result) == 2
    assert result["transaction_date"].tolist() == [
        pd.Timestamp("2021-11-12").date(),
        pd.Timestamp("2021-11-11").date(),
    ]


def test_parse_excel_uses_negative_balance_column(monkeypatch, tmp_path):
    """CaixaBank stores negative running balances in column 9 (Saldo -)."""
    header = [None] * 25
    header[4] = "F. Operación"
    header[5] = "F. Valor"
    header[8] = "Saldo (+)"
    header[9] = "Saldo (-)"

    positive_balance = [None] * 25
    positive_balance[1] = "2100 5602 67 1300043018"
    positive_balance[3] = "EUR"
    positive_balance[4] = "29/10/2021"
    positive_balance[5] = "29/10/2021"
    positive_balance[7] = 11.20
    positive_balance[8] = 1184.93

    negative_balance = positive_balance.copy()
    negative_balance[4] = "29/10/2021"
    negative_balance[7] = 36.36
    negative_balance[8] = None
    negative_balance[9] = -131.47

    raw = pd.DataFrame([[None] * 25, header, positive_balance,
                        negative_balance])
    monkeypatch.setattr(pd, "read_excel", lambda *args, **kwargs: raw)

    source = tmp_path / "movimientos.xls"
    source.touch()
    result = parse_excel(source)

    assert result["running_balance"].tolist() == [1184.93, -131.47]
