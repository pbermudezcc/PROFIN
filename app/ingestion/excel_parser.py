"""
app/ingestion/excel_parser.py
==============================
FAMILYFIN — CaixaBank Excel Parser

Responsibility:
  Read a CaixaBank .xlsx export file (no traditional header row) and return
  a clean, typed pandas DataFrame with standardised column names ready for
  validation and import.

CaixaBank file structure (24 unnamed columns, data from row 1 onward):
  Col 0  : always NaN                     → dropped
  Col 1  : IBAN full string               → iban_raw
  Col 2  : branch code                    → dropped
  Col 3  : currency (EUR)                 → currency
  Col 4  : operation date  dd/mm/yyyy     → transaction_date
  Col 5  : value date      dd/mm/yyyy     → value_date
  Col 6  : ingreso amount  (NaN if gasto) → _ingreso_raw
  Col 7  : gasto amount    (NaN if ingr.) → _gasto_raw
  Col 8  : positive running balance       → running_balance
  Col 9  : negative running balance       → running_balance
  Col 10 : concepto común code            → concepto_comun
  Col 11 : concepto propio code           → concepto_propio
  Col 12 : always 000000000000            → dropped
  Col 13 : bank reference                 → dropped
  Col 14 : issuer / operation description → issuer_name
  Col 15-22 : complementary details       → concept  (concatenated, stripped)
  Col 23 : NaN / extra                    → dropped

Output columns (standardised):
  transaction_date  : datetime.date
  value_date        : datetime.date | None
  amount            : float  (positive = income, negative = expense)
  running_balance   : float
  concepto_comun    : str | None
  concepto_propio   : str | None
  issuer_name       : str | None
  concept           : str | None  (concat of cols 14-22)
  raw_col14         : str | None  (col 14 exactly as received)
  raw_details       : str | None  (cols 15-22 as received, uncollapsed)
  currency          : str         (always 'EUR' for CaixaBank ES)
  iban_raw          : str | None
"""
from __future__ import annotations

import logging
logger = logging.getLogger(__name__)
import re
import unicodedata
from pathlib import Path
from typing import Union

import pandas as pd


# ---------------------------------------------------------------------------
# Column indices (0-based) in the raw CaixaBank .xlsx
# ---------------------------------------------------------------------------
_COL_IBAN            = 1
_COL_CURRENCY        = 3
_COL_DATE_OP         = 4
_COL_DATE_VAL        = 5
_COL_INGRESO         = 6
_COL_GASTO           = 7
_COL_SALDO_POSITIVO  = 8
_COL_SALDO_NEGATIVO  = 9
_COL_CONCEPTO_COMUN  = 10
_COL_CONCEPTO_PROPIO = 11
_COL_DETAILS_START   = 14   # issuer + details cols 14-22
_COL_DETAILS_END     = 22   # inclusive

# Row offset handled dynamically by preamble detection in parse_excel()
# _DATA_START_ROW removed — CaixaBank .xls has variable preamble length

# Date format used by CaixaBank
_DATE_FORMAT = "%d/%m/%Y"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_excel(file_path: Union[str, Path]) -> pd.DataFrame:
    """
    Read a CaixaBank .xlsx export and return a clean, typed DataFrame.

    Parameters
    ----------
    file_path : str or Path
        Path to the .xlsx file.

    Returns
    -------
    pd.DataFrame with columns:
        transaction_date, value_date, amount, running_balance,
        concepto_comun, concepto_propio, issuer_name, concept,
        raw_col14, currency, iban_raw

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the file cannot be read as a valid Excel workbook.
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Fichero no encontrado: {file_path}"
        )

    # -- Read raw: no header, all columns as object to avoid type coercion --
    # Determine engine based on file extension
    suffix = Path(str(file_path)).suffix.lower().strip()
    if suffix == ".xls":
        _engine = "xlrd"
    elif suffix == ".xlsx":
        _engine = "openpyxl"
    else:
        raise ValueError(
            f"Formato de fichero no soportado: '{suffix}'. "
            "FAMILYFIN acepta ficheros .xls y .xlsx de CaixaBank."
        )

    logger.info(
        "Leyendo fichero '%s' con engine='%s'",
        file_path.name, _engine
    )

    try:
        raw: pd.DataFrame = pd.read_excel(
            file_path,
            header=None,
            dtype=object,
            engine=_engine,        # ← solo una vez
        )
        
    except Exception as exc:
        raise ValueError(
            f"No se puede leer el fichero Excel '{file_path.name}': {exc}"
        ) from exc

    # -- Locate and strip preamble + real header row -----------------------
    # CaixaBank .xls structure (xlrd, header=None):
    #   row 0 → empty
    #   row 1 → "MOVIMIENTOS DESDE..." (title)
    #   row 2 → empty
    #   row 3 → "F. Operación | F. Valor | ..."  ← REAL HEADER
    #   row 4 → first transaction
    #   row N → empty (trailing)
    # We detect all header rows by their operation-date cell
    # (accent-insensitive, handles repeated headers in paginated exports).

    header_row_indices = [
        _i for _i in range(len(raw))
        if _is_header_row(raw.iloc[_i])
    ]
    header_row_idx = header_row_indices[0] if header_row_indices else None

    if header_row_idx is not None:
        logger.info(
            "Preámbulo detectado — cabecera en fila %d "
            "(%d filas de preámbulo eliminadas)",
            header_row_idx, header_row_idx + 1,
        )
        raw = raw.iloc[header_row_idx + 1:].reset_index(drop=True)
        repeated_headers = [
            _i for _i in range(len(raw))
            if _is_header_row(raw.iloc[_i])
        ]
        if repeated_headers:
            logger.info(
                "Eliminando %d cabecera(s) repetida(s) del fichero",
                len(repeated_headers),
            )
            raw = raw.drop(index=repeated_headers).reset_index(drop=True)
    else:
        logger.warning(
            "Cabecera 'F. Operación' no encontrada "
            "— continuando sin eliminar preámbulo. "
            "Valor col[4] fila 0: %s",
            str(raw.iloc[0, 4]) if len(raw) > 0 else "N/A",
        )

    # -- Drop fully empty rows (trailing blank + any mid-file gaps) --------
    raw = raw.dropna(how="all").reset_index(drop=True)

    logger.info("Filas de datos tras limpieza: %d", len(raw))

    data = raw.copy()

    if data.empty:
        raise ValueError(
            "El fichero no contiene filas de datos tras omitir la cabecera."
        )

    # -- Validate minimum column count -------------------------------------
    if data.shape[1] < 23:
        raise ValueError(
            f"El fichero tiene {data.shape[1]} columnas — "
            "se esperan al menos 23. "
            "¿Es un export de CaixaBank con el formato correcto?"
        )

    # -- Extract & clean each field ----------------------------------------
    out = pd.DataFrame()

    out["iban_raw"] = (
        data.iloc[:, _COL_IBAN]
        .astype(str)
        .str.strip()
        .replace("nan", None)
    )

    out["currency"] = (
        data.iloc[:, _COL_CURRENCY]
        .astype(str)
        .str.strip()
        .replace("nan", None)
    )

    out["transaction_date"] = _parse_dates(
        data.iloc[:, _COL_DATE_OP], "Fecha operación (col 4)"
    )
    out["value_date"] = _parse_dates(
        data.iloc[:, _COL_DATE_VAL], "Fecha valor (col 5)",
        required=False
    )

    # Unify ingreso / gasto → signed amount
    # ingreso (col 6): positive float, NaN if not an income row
    # gasto   (col 7): positive float, NaN if not an expense row
    # Rule: amount = +ingreso if ingreso not NaN, else -gasto
    ingreso = _to_numeric(data.iloc[:, _COL_INGRESO], "Ingreso (col 6)")
    gasto   = _to_numeric(data.iloc[:, _COL_GASTO],   "Gasto (col 7)")
    # Money is kept to whole cents so duplicate keys and sums are exact.
    out["amount"] = ingreso.combine_first(-gasto).round(2)

    # CaixaBank exports the running balance in two mutually exclusive
    # columns: "Saldo (+)" and "Saldo (-)".  Negative balances are therefore
    # not a malformed/missing value; they are stored in column 9.
    saldo_positivo = _to_numeric(
        data.iloc[:, _COL_SALDO_POSITIVO], "Saldo (+) (col 8)"
    )
    saldo_negativo = _to_numeric(
        data.iloc[:, _COL_SALDO_NEGATIVO], "Saldo (-) (col 9)"
    )
    out["running_balance"] = saldo_positivo.combine_first(saldo_negativo).round(2)

    out["concepto_comun"] = _clean_str(
        data.iloc[:, _COL_CONCEPTO_COMUN]
    )
    out["concepto_propio"] = _clean_str(
        data.iloc[:, _COL_CONCEPTO_PROPIO]
    )

    # raw_col14: col 14 exactly as received (before stripping)
    out["raw_col14"] = (
        data.iloc[:, 14]
        .astype(str)
        .str.strip()
        .replace("nan", None)
    )

    # issuer_name: extract from col 14 — remove leading "Fecha de operación: DD-MM-YYYY"
    out["issuer_name"] = out["raw_col14"].apply(_extract_issuer)

    # concept: concatenate cols 14-22 (strip, drop empty, join with space)
    concept_cols = data.iloc[
        :, _COL_DETAILS_START : _COL_DETAILS_END + 1
    ]
    out["concept"] = concept_cols.apply(
        _concat_details, axis=1
    )

    # raw_details: complementary cols 15-22 exactly as received (only
    # stripped), kept for traceability before any cleaning.
    out["raw_details"] = data.iloc[
        :, _COL_DETAILS_START + 1 : _COL_DETAILS_END + 1
    ].apply(_raw_details, axis=1)

    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_header_row(row: pd.Series) -> bool:
    """Return whether a raw row is a CaixaBank operation header."""
    if len(row) <= _COL_DATE_OP:
        return False
    value = str(row.iloc[_COL_DATE_OP]).strip()
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).lower()
    return bool(re.match(r"^f\s*\.?\s*operaci", normalized))


def _parse_dates(
    series: pd.Series,
    field_name: str,
    required: bool = True,
) -> pd.Series:
    """
    Parse a date column from a CaixaBank export.

    Handles three input variants produced by different Excel engines:
      - datetime / Timestamp objects  → xlrd converts .xls dates automatically
      - string "dd/mm/yyyy"           → standard CaixaBank text format
      - string "yyyy-mm-dd ..."       → ISO format (edge cases / openpyxl)
      - NaN / None / "nan"            → treated as missing

    Returns a Series of datetime.date objects (or None if not required).
    """
    def _coerce_one(val: object) -> "pd.Timestamp | None":
        """Convert a single cell value to a Timestamp, or NaT on failure."""
        import datetime as _dt

        if val is None or (isinstance(val, float) and pd.isna(val)):
            return pd.NaT

        # Already a date/datetime object (xlrd auto-converts .xls date cells)
        if isinstance(val, (_dt.datetime, _dt.date)):
            return pd.Timestamp(val)

        # Pandas Timestamp
        if isinstance(val, pd.Timestamp):
            return val

        s = str(val).strip()
        if not s or s.lower() in ("nan", "none", "nat"):
            return pd.NaT

        # Try the canonical CaixaBank format first
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"):
            try:
                return pd.Timestamp(
                    _dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
                )
            except ValueError:
                continue

        # Last resort — let pandas guess
        try:
            return pd.to_datetime(s, dayfirst=True)
        except Exception:
            return pd.NaT

    parsed = series.apply(_coerce_one)

    if required and parsed.isna().any():
        bad_count = int(parsed.isna().sum())
        # Log which rows are failing to help diagnose edge cases
        bad_vals = series[parsed.isna()].tolist()
        logger.warning(
            "_parse_dates '%s': %d unparseable value(s): %s",
            field_name, bad_count, bad_vals[:5]
        )
        raise ValueError(
            f"Campo '{field_name}': {bad_count} fila(s) con fecha "
            "no válida o formato inesperado (se esperaba dd/mm/yyyy). "
            f"Valores problemáticos: {[str(v) for v in bad_vals[:3]]}"
        )
    return parsed.dt.date


def _to_numeric(series: pd.Series, field_name: str) -> pd.Series:
    """
    Convert a column to float. Treats blank / 'nan' strings as NaN.
    Does NOT raise on NaN — callers decide if NaN is acceptable.
    """
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(",", ".", regex=False)  # handle European decimal comma
        .replace({"nan": None, "": None, "None": None})
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _clean_str(series: pd.Series) -> pd.Series:
    """Strip whitespace, collapse internal spaces, return None for blank/nan."""
    return (
        series.astype(str)
        .str.strip()
        .str.replace(r"\s+", " ", regex=True)
        .replace({"nan": None, "": None, "None": None})
    )


_DATE_PREFIX_RE = re.compile(
    r"^Fecha\s+de\s+operaci[oó]n\s*:\s*\d{2}-\d{2}-\d{4}\s*",
    re.IGNORECASE,
)


def _extract_issuer(raw: object) -> str | None:
    """
    Extract the issuer name from col 14.
    CaixaBank col 14 has two patterns:
      a) "Fecha de operación: DD-MM-YYYY   ISSUER NAME"
         → strip the date prefix, return stripped issuer name
      b) Plain issuer name with no date prefix
         → return as-is (stripped)
    Returns None if blank/nan.
    """
    if not raw or str(raw).strip() in ("", "nan", "None"):
        return None
    text = str(raw).strip()
    text = _DATE_PREFIX_RE.sub("", text).strip()
    # Collapse multiple internal spaces (CaixaBank pads with spaces)
    text = re.sub(r"\s{2,}", " ", text)
    return text if text else None


def _raw_details(row: pd.Series) -> str | None:
    """Join the non-empty raw detail cells with ' | ' without cleaning them."""
    parts = [
        str(val).strip()
        for val in row
        if str(val).strip() not in ("", "nan", "None")
    ]
    return " | ".join(parts) if parts else None


def _concat_details(row: pd.Series) -> str | None:
    """
    Concatenate detail columns 14-22 into a single concept string.
    Strips each cell, drops empty/nan, joins with single space.
    Returns None if all cells are empty.
    """
    parts = []
    for val in row:
        s = str(val).strip()
        if s and s not in ("nan", "None"):
            # Collapse internal spaces
            s = re.sub(r"\s{2,}", " ", s)
            parts.append(s)
    return " | ".join(parts) if parts else None