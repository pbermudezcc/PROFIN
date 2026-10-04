"""
app/investments/importers/service.py
====================================
FAMILYFIN — Turn a parsed broker statement into ledger operations.

Flow used by the import dialog:
  1. parse_any(path)            DEGIRO or MyInvestor, detected from the header (local)
  2. suggest_symbols(provider, statement.products, catalogue_isins(session))
                                (symbols.py, network → worker)
  3. user reviews / edits each product's symbol; for MyInvestor also the
     kind of each movement and the estimated quantities
  4. fetch_closes(provider, statement, symbols) + estimate_quantities()
                                (only when quantities are missing; network → worker)
  5. fetch_security_infos(provider, symbols not yet catalogued) (network → worker)
  6. apply_import(session, user_id, statement, symbols, infos)
                                (GUI thread: creates the securities, builds
     drafts and calls ledger.import_drafts, which skips operations imported
     before and writes everything or nothing)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.db.models import InvestmentKind, Security
from app.investments.importers import degiro, myinvestor
from app.investments.importers.base import (
    KIND_BUY,
    KIND_DIVIDEND,
    KIND_IGNORE,
    KIND_SELL,
    is_isin,
)
from app.investments.importers.degiro import StatementError
from app.investments.ledger import ImportResult, TransactionDraft, existing_refs, import_drafts
from app.investments.market_data import MarketDataProvider, SecurityInfo
from app.investments.portfolio import LedgerError
from app.investments.prices import fx_rate, security_from_info

_KINDS = {KIND_BUY: InvestmentKind.buy, KIND_SELL: InvestmentKind.sell,
          KIND_DIVIDEND: InvestmentKind.dividend}


def parse_any(path):
    """Read a DEGIRO or MyInvestor statement, whichever the file is."""
    path = Path(path)
    if myinvestor.looks_like_myinvestor(path):
        return myinvestor.parse_statement(path)
    try:
        return degiro.parse_statement(path)
    except StatementError as exc:
        raise StatementError(
            "No se reconoce el archivo. Se admiten el «Estado de cuenta» de DEGIRO y los "
            "movimientos de cuenta de MyInvestor (.xlsx o .csv).") from exc


def catalogue_isins(session: Session) -> Dict[str, str]:
    """ISIN → symbol of the securities already in the catalogue."""
    return {isin: symbol for isin, symbol in
            session.query(Security.isin, Security.symbol).filter(Security.isin.isnot(None))}


def already_imported(session: Session, user_id: int, statement) -> set:
    return existing_refs(session, user_id, statement.broker)


def fetch_security_infos(provider: MarketDataProvider, symbols: Iterable[str]) -> Dict[str, SecurityInfo]:
    """Provider data for new catalogue rows (raises MarketDataError)."""
    return {symbol: provider.security_info(symbol) for symbol in sorted(set(symbols))}


def symbols_to_fetch(session: Session, symbols: Iterable[str]) -> List[str]:
    known = {s for (s,) in session.query(Security.symbol)}
    return sorted({s.strip().upper() for s in symbols if s.strip()} - known)


def fetch_closes(provider: MarketDataProvider, statement, symbols: Dict[str, str]):
    """Closes needed to estimate missing quantities (network)."""
    start, end = myinvestor.price_window(statement)
    wanted = sorted({symbols[r.key] for r in statement.rows
                     if r.quantity_estimated and symbols.get(r.key)})
    if start is None or not wanted:
        return {}
    return provider.daily_closes(wanted, start=start, end=end)


def estimate_quantities(statement, symbols: Dict[str, str], closes) -> None:
    myinvestor.estimate_quantities(
        statement.rows, {k: (s or "").strip().upper() for k, s in symbols.items() if s}, closes,
        {k: bool(p.get("fund")) for k, p in statement.products.items()})


@dataclass
class ImportSummary:
    result:     ImportResult
    trades:     int
    dividends:  int
    estimated:  int          # dividends whose EUR amount had to be estimated
    ignored:    int = 0      # rows the user (or the reader) marked "ignore"


def build_drafts(session: Session, statement, security_ids: Dict[str, int]) -> Tuple[list, int, int]:
    """(drafts, dividends estimated with a cached FX rate, rows ignored)."""
    drafts: List[TransactionDraft] = []
    estimated = ignored = 0
    broker = statement.broker
    for row in statement.rows:
        if row.kind == KIND_IGNORE:
            ignored += 1
            continue
        notes = f"Importado de {broker}" + (f" ({row.note})" if row.note else "")
        if row.kind in (KIND_BUY, KIND_SELL):
            if not row.quantity:
                raise LedgerError(
                    f"Falta el número de acciones de «{row.name}» del {row.day:%d/%m/%Y}.")
            drafts.append(TransactionDraft(
                security_id=security_ids[row.key], kind=_KINDS[row.kind], trade_date=row.day,
                broker=broker, quantity=row.quantity,
                price=round(row.price, 4) if row.price else None, currency=row.currency,
                amount_eur=row.amount_eur, fees_eur=row.fees_eur, notes=notes,
                broker_ref=row.ref))
            continue
        net_eur, withheld_eur = row.amount_eur, row.withheld_eur
        if net_eur is None:
            rate = fx_rate(session, row.currency, on=row.day)
            if not rate or row.foreign_gross is None:
                raise LedgerError(
                    f"No hay tipo de cambio {row.currency}/EUR para el dividendo de "
                    f"{row.name} del {row.day:%d/%m/%Y}. Actualiza las cotizaciones y "
                    "vuelve a intentarlo.")
            net_eur = round((row.foreign_gross - row.foreign_withheld) / rate, 2)
            withheld_eur = round(row.foreign_withheld / rate, 2)
            estimated += 1
        if row.gross_text:
            notes = f"Importado de {broker}: {row.gross_text}"
        drafts.append(TransactionDraft(
            security_id=security_ids[row.key], kind=InvestmentKind.dividend, trade_date=row.day,
            broker=broker, amount_eur=net_eur, tax_withheld_eur=withheld_eur or 0.0,
            currency=row.currency, notes=notes, broker_ref=row.ref))
    return drafts, estimated, ignored


def apply_import(
    session: Session,
    user_id: int,
    statement,
    symbols: Dict[str, str],
    infos: Optional[Dict[str, SecurityInfo]] = None,
) -> ImportSummary:
    """Create missing securities and import the statement for one user."""
    infos = infos or {}
    security_ids: Dict[str, int] = {}
    used_keys = {r.key for r in statement.rows if r.kind != KIND_IGNORE}
    try:
        for key, symbol in symbols.items():
            if key not in used_keys:
                continue
            symbol = symbol.strip().upper()
            if not symbol:
                raise LedgerError(f"Falta el símbolo de «{statement.products[key]['name']}».")
            security = session.query(Security).filter(Security.symbol == symbol).one_or_none()
            isin = key if is_isin(key) else None
            if security is None:
                info = infos.get(symbol)
                if info is None:
                    raise LedgerError(f"Faltan los datos del valor {symbol}.")
                security = security_from_info(session, info, isin)
            elif isin and not security.isin:
                security.isin = isin
            security_ids[key] = security.id
        drafts, estimated, ignored = build_drafts(session, statement, security_ids)
    except Exception:
        session.rollback()
        raise
    try:
        result = import_drafts(session, user_id, drafts)   # commits the new securities too
    except Exception:
        session.rollback()          # also drops securities created for this import
        raise
    added = result.added
    return ImportSummary(
        result=result,
        trades=sum(1 for t in added if InvestmentKind(t.kind) != InvestmentKind.dividend),
        dividends=sum(1 for t in added if InvestmentKind(t.kind) == InvestmentKind.dividend),
        estimated=estimated,
        ignored=ignored,
    )
