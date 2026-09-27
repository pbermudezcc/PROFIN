"""
app/investments/importers/service.py
====================================
FAMILYFIN — Turn a parsed broker statement into ledger operations.

Flow used by the import dialog:
  1. parse_statement(path)                      (degiro.py, local)
  2. suggest_symbols(provider, products, catalogue_isins(session))
                                                (symbols.py, network → worker)
  3. user reviews / edits the ISIN → symbol map
  4. fetch_security_infos(provider, symbols not yet catalogued)
                                                (network → worker)
  5. apply_import(session, user_id, statement, symbols, infos)
                                                (GUI thread: creates the
     securities, builds drafts and calls ledger.import_drafts, which skips
     operations imported before and writes everything or nothing)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.db.models import InvestmentKind, Security
from app.investments.importers.degiro import ParsedStatement
from app.investments.ledger import ImportResult, TransactionDraft, existing_refs, import_drafts
from app.investments.market_data import MarketDataProvider, SecurityInfo
from app.investments.portfolio import LedgerError
from app.investments.prices import fx_rate, security_from_info


def catalogue_isins(session: Session) -> Dict[str, str]:
    """ISIN → symbol of the securities already in the catalogue."""
    return {isin: symbol for isin, symbol in
            session.query(Security.isin, Security.symbol).filter(Security.isin.isnot(None))}


def already_imported(session: Session, user_id: int, statement: ParsedStatement) -> set:
    return existing_refs(session, user_id, statement.broker)


def fetch_security_infos(provider: MarketDataProvider, symbols: Iterable[str]) -> Dict[str, SecurityInfo]:
    """Provider data for new catalogue rows (raises MarketDataError)."""
    return {symbol: provider.security_info(symbol) for symbol in sorted(set(symbols))}


def symbols_to_fetch(session: Session, symbols: Iterable[str]) -> List[str]:
    known = {s for (s,) in session.query(Security.symbol)}
    return sorted({s.strip().upper() for s in symbols} - known)


@dataclass
class ImportSummary:
    result:     ImportResult
    trades:     int
    dividends:  int
    estimated:  int          # dividends whose EUR amount had to be estimated


def build_drafts(
    session: Session,
    statement: ParsedStatement,
    security_ids: Dict[str, int],
) -> tuple:
    """(drafts, number of dividends estimated with a cached FX rate)."""
    drafts: List[TransactionDraft] = []
    estimated = 0
    broker = statement.broker
    for trade in statement.trades:
        notes = f"Importado de {broker}"
        if trade.fills > 1:
            notes += f" ({trade.fills} ejecuciones)"
        drafts.append(TransactionDraft(
            security_id=security_ids[trade.isin],
            kind=InvestmentKind.buy if trade.kind == "buy" else InvestmentKind.sell,
            trade_date=trade.trade_date, broker=broker, quantity=trade.quantity,
            price=round(trade.price, 4), currency=trade.currency, amount_eur=trade.amount_eur,
            fees_eur=trade.fees_eur, notes=notes, broker_ref=trade.ref,
        ))
    for dividend in statement.dividends:
        net_eur, withheld_eur = dividend.net_eur, dividend.withheld_eur
        if net_eur is None:
            rate = fx_rate(session, dividend.currency, on=dividend.pay_date)
            if not rate:
                raise LedgerError(
                    f"No hay tipo de cambio {dividend.currency}/EUR para el dividendo de "
                    f"{dividend.name} del {dividend.pay_date:%d/%m/%Y}. Actualiza las "
                    "cotizaciones y vuelve a intentarlo.")
            net_eur = round((dividend.gross - dividend.withheld) / rate, 2)
            withheld_eur = round(dividend.withheld / rate, 2)
            estimated += 1
        drafts.append(TransactionDraft(
            security_id=security_ids[dividend.isin], kind=InvestmentKind.dividend,
            trade_date=dividend.pay_date, broker=broker, amount_eur=net_eur,
            tax_withheld_eur=withheld_eur or 0.0, currency=dividend.currency,
            notes=f"Importado de {broker}: {dividend.gross:.2f} {dividend.currency} brutos",
            broker_ref=dividend.ref,
        ))
    return drafts, estimated


def apply_import(
    session: Session,
    user_id: int,
    statement: ParsedStatement,
    symbols: Dict[str, str],
    infos: Optional[Dict[str, SecurityInfo]] = None,
) -> ImportSummary:
    """Create missing securities and import the statement for one user."""
    infos = infos or {}
    security_ids: Dict[str, int] = {}
    try:
        for isin, symbol in symbols.items():
            symbol = symbol.strip().upper()
            security = session.query(Security).filter(Security.symbol == symbol).one_or_none()
            if security is None:
                info = infos.get(symbol)
                if info is None:
                    raise LedgerError(f"Faltan los datos del valor {symbol}.")
                security = security_from_info(session, info, isin)
            elif not security.isin:
                security.isin = isin
            security_ids[isin] = security.id
        drafts, estimated = build_drafts(session, statement, security_ids)
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
    )
