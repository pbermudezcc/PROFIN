"""Inversiones: positions, evolution, allocation, operations and tax report.

Pages show the cached prices. «Actualizar cotizaciones» asks the PC to
download new ones in the background (app/web/price_refresh.py); the page
polls until it ends and then reloads its content.
"""
from __future__ import annotations

import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db.models import AssetType, AuditAction, InvestmentKind, Security
from app.investments.benchmarks import BENCHMARKS, DEFAULT_BENCHMARK
from app.investments.ledger import (
    KIND_LABELS,
    TransactionDraft,
    delete_transaction,
    get_transaction,
    known_brokers,
    list_transactions,
    picker_securities,
    save_transaction,
)
from app.investments.performance import ALLOCATION_DIMENSIONS, allocation, portfolio_history
from app.investments.portfolio import LedgerError
from app.investments.snapshot import portfolio_snapshot
from app.investments.tax_report import export_xlsx, report_years, tax_report
from app.web.deps import DB, USER, WebUser, notify_change
from app.web.price_refresh import RECENT, STARTED, start_price_refresh
from app.web.render import audit, hx_trigger, is_htmx, parse_date, parse_float, parse_int, render

logger = logging.getLogger(__name__)
router = APIRouter()

TABS = [
    ("posiciones", "Posiciones", "/inversiones"),
    ("evolucion", "Evolución", "/inversiones/evolucion"),
    ("distribucion", "Distribución", "/inversiones/distribucion"),
    ("operaciones", "Operaciones", "/inversiones/operaciones"),
    ("fiscal", "Fiscalidad", "/inversiones/fiscal"),
]
HISTORY_PERIODS = [("1m", "1M", 31), ("6m", "6M", 183), ("ytd", "Año", "ytd"),
                   ("1a", "1A", 365), ("3a", "3A", 3 * 365), ("todo", "Todo", None)]
ASSET_LABELS = {AssetType.stock: "Acción", AssetType.etf: "ETF", AssetType.fund: "Fondo"}


def _price_status(request: Request, recent: bool = False) -> Dict[str, Any]:
    job = request.app.state.price_refresh
    return {"running": job.running, "result": job.last_result, "recent": recent}


def _page(request: Request, template: str, tab: str, user: WebUser, ctx: Dict[str, Any]):
    ctx.update({"tabs": TABS, "tab": tab, "pr": _price_status(request)})
    return render(request, template, ctx, user=user, active="inversiones")


# ---------------------------------------------------------------------------
# Price refresh (runs on the PC)
# ---------------------------------------------------------------------------

@router.post("/inversiones/cotizaciones/actualizar")
def refresh_prices(request: Request, user: WebUser = USER, db: Session = DB) -> Response:
    outcome = start_price_refresh(request.app, user.id)
    if outcome == STARTED:
        audit(db, request, user, "price_history", None, AuditAction.update, new="actualizar cotizaciones")
    if not is_htmx(request):
        return RedirectResponse("/inversiones", status_code=303)
    return render(request, "partials/price_status.html",
                  {"pr": _price_status(request, recent=outcome == RECENT)}, user=user)


@router.get("/inversiones/cotizaciones/estado")
def refresh_status(request: Request, user: WebUser = USER) -> Response:
    """Polled while a refresh runs; when it has ended, reload the page content."""
    pr = _price_status(request)
    response = render(request, "partials/price_status.html", {"pr": pr}, user=user)
    result = pr["result"]
    if not pr["running"] and result is not None:
        if result.ok:
            hx_trigger(response, toast="Cotizaciones actualizadas", refreshPage=True)
        else:
            hx_trigger(response, toastError="No se pudieron actualizar las cotizaciones")
    return response


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

@router.get("/inversiones")
def positions(request: Request, cerradas: int = 0, user: WebUser = USER, db: Session = DB) -> Response:
    snapshot = portfolio_snapshot(db, user.id)
    rows = sorted(snapshot.open_rows, key=lambda r: -r.market_value_eur)
    closed = sorted(snapshot.closed_rows, key=lambda r: r.security.name.lower()) if cerradas else []
    return _page(request, "inversiones.html", "posiciones", user, {
        "snap": snapshot, "t": snapshot.totals, "rows": rows, "closed": closed,
        "show_closed": bool(cerradas), "has_closed": bool(snapshot.closed_rows),
        "asset_labels": ASSET_LABELS,
    })


@router.get("/api/inversiones/posiciones")
def positions_api(user: WebUser = USER, db: Session = DB) -> JSONResponse:
    snapshot = portfolio_snapshot(db, user.id)
    t = snapshot.totals
    return JSONResponse({
        "as_of": snapshot.as_of.isoformat(),
        "totals": {
            "market_value_eur": t.market_value_eur, "cost_basis_eur": t.cost_basis_eur,
            "cash_eur": t.cash_eur, "total_value_eur": t.total_value_eur,
            "unrealized_gain_eur": t.unrealized_gain_eur, "unrealized_pct": t.unrealized_pct,
            "day_change_eur": t.day_change_eur, "day_change_pct": t.day_change_pct,
            "realized_ytd_eur": t.realized_ytd_eur, "dividends_ytd_eur": t.dividends_ytd_eur,
            "xirr": t.xirr, "open_positions": t.open_positions,
        },
        "positions": [
            {"security_id": r.security.id, "symbol": r.security.symbol, "name": r.security.name,
             "quantity": r.quantity, "price": r.price, "currency": r.security.currency,
             "price_date": r.price_date.isoformat() if r.price_date else None,
             "market_value_eur": r.market_value_eur, "cost_basis_eur": r.cost_basis_eur,
             "unrealized_gain_eur": r.unrealized_gain_eur, "unrealized_pct": r.unrealized_pct,
             "weight": r.weight, "brokers": r.brokers}
            for r in snapshot.rows
        ],
    })


# ---------------------------------------------------------------------------
# Evolution / allocation
# ---------------------------------------------------------------------------

_MONTHS = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _day_label(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month]} {day.year % 100:02d}"


def _history_chart(db: Session, user_id: int, period_key: str, benchmark: str) -> Dict[str, Any]:
    bench_key = benchmark if benchmark in BENCHMARKS else None
    history = portfolio_history(db, user_id, benchmark_key=bench_key)
    start: Optional[date] = None
    if history.points:
        end = history.points[-1].day
        days = {k: d for k, _, d in HISTORY_PERIODS}.get(period_key, 365)
        if days == "ytd":
            start = date(end.year, 1, 1)
        elif days is not None:
            start = end - timedelta(days=days)
    points = history.since(start)
    # One point per week beyond ~400 points keeps the phone chart smooth.
    step = max(1, len(points) // 400)
    sampled = points[::step]
    if points and sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    series = [
        {"name": "Valor de la cartera", "color": "investment", "values": [round(p.value_eur, 2) for p in sampled]},
        {"name": "Capital invertido", "color": "muted", "dashed": True,
         "values": [round(p.invested_eur, 2) for p in sampled]},
    ]
    if history.benchmark_name and any(p.benchmark_eur is not None for p in sampled):
        series.append({"name": f"Mismo dinero en {history.benchmark_name}", "color": "accent",
                       "values": [None if p.benchmark_eur is None else round(p.benchmark_eur, 2)
                                  for p in sampled]})
    first, last = (points[0], points[-1]) if points else (None, None)
    return {
        "chart": {"type": "lines", "labels": [_day_label(p.day) for p in sampled], "series": series},
        "first": first,
        "last": last,
        "benchmark_name": history.benchmark_name,
    }


@router.get("/inversiones/evolucion")
def evolution(
    request: Request, periodo: str = "1a", benchmark: str = DEFAULT_BENCHMARK,
    user: WebUser = USER, db: Session = DB,
) -> Response:
    data = _history_chart(db, user.id, periodo, benchmark)
    last = data["last"]
    gain = (last.value_eur - last.invested_eur) if last else None
    return _page(request, "inv_evolucion.html", "evolucion", user, {
        **data,
        "periodo": periodo if periodo in {k for k, _, _ in HISTORY_PERIODS} else "1a",
        "periods": [(k, label) for k, label, _ in HISTORY_PERIODS],
        "benchmark": benchmark,
        "benchmarks": [("", "Sin índice")] + [(k, b.name) for k, b in BENCHMARKS.items()],
        "gain": gain,
    })


@router.get("/api/inversiones/evolucion")
def evolution_api(periodo: str = "1a", benchmark: str = DEFAULT_BENCHMARK,
                  user: WebUser = USER, db: Session = DB) -> JSONResponse:
    return JSONResponse({"chart": _history_chart(db, user.id, periodo, benchmark)["chart"]})


@router.get("/inversiones/distribucion")
def distribution(request: Request, por: str = "security", user: WebUser = USER, db: Session = DB) -> Response:
    por = por if por in ALLOCATION_DIMENSIONS else "security"
    items = allocation(db, user.id, por)
    total = sum(v for _, v in items) or 1.0
    return _page(request, "inv_distribucion.html", "distribucion", user, {
        "por": por,
        "dimensions": list(ALLOCATION_DIMENSIONS.items()),
        "items": [(label, value, value / total) for label, value in items],
        "total": sum(v for _, v in items),
        "chart": {"type": "doughnut", "labels": [k for k, _ in items], "values": [round(v, 2) for _, v in items]},
    })


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _kind(value: Optional[str]) -> Optional[InvestmentKind]:
    return InvestmentKind(value) if value in InvestmentKind._value2member_map_ else None


@router.get("/inversiones/operaciones")
def operations(
    request: Request, tipo: Optional[str] = None, broker: Optional[str] = None, q: Optional[str] = None,
    valor: Optional[int] = None, user: WebUser = USER, db: Session = DB,
) -> Response:
    rows = list_transactions(db, user.id, kind=_kind(tipo), broker=broker or None, text=(q or "")[:100] or None)
    if valor:
        rows = [r for r in rows if r.security_id == valor]
    securities = {s.id: s for s in db.query(Security).filter(Security.id.in_({r.security_id for r in rows}))}
    return _page(request, "inv_operaciones.html", "operaciones", user, {
        "rows": rows, "securities": securities, "kind_labels": KIND_LABELS,
        "kinds": [(k.value, label) for k, label in KIND_LABELS.items()],
        "brokers": known_brokers(db, user.id), "tipo": tipo or "", "broker": broker or "", "q": q or "",
        "valor": valor,
    })


def _form_context(db: Session, user: WebUser, txn=None, error: Optional[str] = None,
                  values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    securities = picker_securities(db, user.id)
    own = {sid for (sid,) in db.query(Security.id).filter(Security.id.in_(
        [t.security_id for t in list_transactions(db, user.id)]))}
    return {
        "txn": txn,
        "error": error,
        "v": values or {},
        "kinds": [(k.value, label) for k, label in KIND_LABELS.items()],
        "own_securities": [s for s in securities if s.id in own],
        "other_securities": [s for s in securities if s.id not in own],
        "brokers": known_brokers(db, user.id),
    }


def _values_from_txn(txn) -> Dict[str, Any]:
    def num(value):
        return "" if value is None else f"{value:g}"
    return {
        "kind": InvestmentKind(txn.kind).value, "security_id": txn.security_id,
        "trade_date": txn.trade_date.isoformat(), "broker": txn.broker,
        "quantity": num(txn.quantity), "price": num(txn.price), "currency": txn.currency or "",
        "amount_eur": num(txn.amount_eur), "fees_eur": num(txn.fees_eur),
        "tax_withheld_eur": num(txn.tax_withheld_eur), "cash_paid_eur": num(txn.cash_paid_eur),
        "split_ratio": num(txn.split_ratio), "notes": txn.notes or "",
    }


@router.get("/inversiones/operaciones/nueva")
def new_operation(request: Request, valor: Optional[int] = None, tipo: Optional[str] = None,
                  user: WebUser = USER, db: Session = DB) -> Response:
    values = {"kind": tipo if _kind(tipo) else "buy", "trade_date": date.today().isoformat(),
              "security_id": valor, "fees_eur": "0"}
    brokers = known_brokers(db, user.id)
    values["broker"] = brokers[0] if brokers else ""
    return render(request, "partials/op_form.html", _form_context(db, user, values=values), user=user)


@router.get("/inversiones/operaciones/{operation_id}")
def edit_operation(request: Request, operation_id: int, user: WebUser = USER, db: Session = DB) -> Response:
    try:
        txn = get_transaction(db, user.id, operation_id)
    except LedgerError:
        raise HTTPException(status_code=404)
    return render(request, "partials/op_form.html",
                  _form_context(db, user, txn=txn, values=_values_from_txn(txn)), user=user)


@router.post("/inversiones/operaciones")
def save_operation(
    request: Request,
    operation_id: str = Form(""),
    kind: str = Form(...),
    security_id: str = Form(""),
    trade_date: str = Form(""),
    broker: str = Form("", max_length=64),
    quantity: str = Form(""),
    price: str = Form(""),
    currency: str = Form("", max_length=3),
    amount_eur: str = Form(""),
    fees_eur: str = Form(""),
    tax_withheld_eur: str = Form(""),
    cash_paid_eur: str = Form(""),
    split_ratio: str = Form(""),
    notes: str = Form("", max_length=1000),
    user: WebUser = USER,
    db: Session = DB,
) -> Response:
    values = dict(kind=kind, security_id=parse_int(security_id), trade_date=trade_date, broker=broker,
                  quantity=quantity, price=price, currency=currency, amount_eur=amount_eur,
                  fees_eur=fees_eur, tax_withheld_eur=tax_withheld_eur, cash_paid_eur=cash_paid_eur,
                  split_ratio=split_ratio, notes=notes)
    existing_id = parse_int(operation_id)
    txn = None
    if existing_id is not None:
        try:
            txn = get_transaction(db, user.id, existing_id)
        except LedgerError:
            raise HTTPException(status_code=404)

    def fail(message: str) -> Response:
        response = render(request, "partials/op_form.html",
                          _form_context(db, user, txn=txn, error=message, values=values),
                          user=user, status_code=200)
        return response

    op_kind = _kind(kind)
    day = parse_date(trade_date)
    if op_kind is None:
        return fail("Elige el tipo de operación.")
    if day is None:
        return fail("La fecha es obligatoria.")
    if day > date.today():
        return fail("La fecha no puede ser futura.")
    security = db.get(Security, values["security_id"]) if values["security_id"] else None
    if security is None:
        return fail("Selecciona un valor.")
    draft = TransactionDraft(
        security_id=security.id,
        kind=op_kind,
        trade_date=day,
        broker=broker,
        quantity=parse_float(quantity),
        price=parse_float(price),
        currency=(currency or security.currency or "EUR").strip()[:3] or None,
        amount_eur=parse_float(amount_eur) or 0.0,
        fees_eur=parse_float(fees_eur) or 0.0,
        tax_withheld_eur=parse_float(tax_withheld_eur) or 0.0,
        cash_paid_eur=parse_float(cash_paid_eur),
        split_ratio=parse_float(split_ratio),
        notes=notes,
        broker_ref=txn.broker_ref if txn is not None else None,
        id=existing_id,
    )
    try:
        saved = save_transaction(db, user.id, draft)
    except LedgerError as exc:
        return fail(str(exc))
    audit(db, request, user, "investment_transactions", saved.id,
          AuditAction.update if existing_id else AuditAction.insert,
          new=f"{op_kind.value} {security.symbol} {day.isoformat()} {draft.amount_eur:.2f} EUR")
    notify_change(request, user, "investments")
    response = Response(status_code=204)
    if is_htmx(request):
        hx_trigger(response, toast="Operación guardada", closeSheet=True, refreshPage=True)
    return response


@router.post("/inversiones/operaciones/{operation_id}/borrar")
def remove_operation(request: Request, operation_id: int, user: WebUser = USER, db: Session = DB) -> Response:
    try:
        txn = get_transaction(db, user.id, operation_id)
    except LedgerError:
        raise HTTPException(status_code=404)
    summary = f"{InvestmentKind(txn.kind).value} security={txn.security_id} {txn.trade_date.isoformat()}"
    try:
        delete_transaction(db, user.id, operation_id)
    except LedgerError as exc:
        return render(request, "partials/op_form.html",
                      _form_context(db, user, txn=txn, error=str(exc), values=_values_from_txn(txn)),
                      user=user)
    audit(db, request, user, "investment_transactions", operation_id, AuditAction.delete, old=summary)
    notify_change(request, user, "investments")
    response = Response(status_code=204)
    hx_trigger(response, toast="Operación eliminada", closeSheet=True, refreshPage=True)
    return response


# ---------------------------------------------------------------------------
# Tax report
# ---------------------------------------------------------------------------

def _years(db: Session, user_id: int) -> List[int]:
    return report_years(db, user_id) or [date.today().year]


@router.get("/inversiones/fiscal")
def tax(request: Request, anio: Optional[int] = None, user: WebUser = USER, db: Session = DB) -> Response:
    years = _years(db, user.id)
    year = anio if anio in years else years[0]
    return _page(request, "inv_fiscal.html", "fiscal", user, {
        "years": years, "year": year, "r": tax_report(db, user.id, year),
    })


@router.get("/inversiones/fiscal/{year}.xlsx")
def tax_xlsx(year: int, user: WebUser = USER, db: Session = DB) -> Response:
    if year not in _years(db, user.id):
        raise HTTPException(status_code=404)
    report = tax_report(db, user.id, year)
    with tempfile.TemporaryDirectory(prefix="familyfin-web-") as folder:
        path = export_xlsx(report, Path(folder) / f"fiscalidad_{year}.xlsx", owner=user.username)
        content = Path(path).read_bytes()
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="FAMILYFIN_fiscalidad_{year}.xlsx"'},
    )
