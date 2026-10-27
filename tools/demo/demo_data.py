"""
tools/demo/demo_data.py
=======================
FAMILYFIN — The DEMO persona and its data, written into whatever database
the app is bound to. Callers choose it:

  * build_demo.py         the isolated sandbox (dist/demo, see _sandbox.py)
  * install_demo_user.py  the family's real database, as user DEMO

The persona: an engineer living in Bilbao, payroll and mortgage at
CaixaBank, invests monthly in an ETF plus a few Spanish/US shares at DEGIRO.
Every figure is FICTITIOUS.

How the data gets in — the same way a real user's does:
  1. One CaixaBank-format statement (.xlsx, 24 columns, preamble) per month
     is written to the statements folder.
  2. Every statement up to 12 October 2026 goes through the real importer
     (parse → validate → duplicates → commit → rules).
  3. A last statement (9–16 October) is left NOT imported; it overlaps the
     previous one so the import flow and the duplicate detection can be
     shown live.
Investments use real historical closes from Yahoo Finance when online, so
the prices the app refreshes later are consistent with the trades.

The securities table is shared by every user: existing securities are
reused untouched, and synthetic (offline) prices only go to the ones this
module creates.
"""
from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook

from app.auth.auth_service import create_user
from app.categorization.category_manager import create_category
from app.categorization.rules_engine import create_rule
from app.db.models import (
    Account, AssetType, Category, CategoryLevel, ImportBatch, InvestmentKind,
    InvestmentTransaction, MatchType, Security, Transaction, TransactionType,
)
from app.ingestion.import_service import run_import
from app.ingestion.importer import prepare_import

START = date(2025, 1, 1)
LAST_IMPORTED_DAY = date(2026, 10, 12)
END = date(2026, 10, 16)
PENDING_FROM = date(2026, 10, 9)        # the pending statement overlaps the last import
IBAN = "ES7621001234560200037314"       # CaixaBank bank code (2100), fictitious account
OPENING_BALANCE = 6_214.37

MONTHS_ES = ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO",
             "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"]

# Concepto común codes used consistently across the statements.
C_CARD, C_BILL, C_PAYROLL, C_TRANSFER, C_BIZUM, C_ATM, C_LOAN, C_FEE = (
    "044", "003", "012", "004", "028", "001", "006", "017")


# ---------------------------------------------------------------------------
# Categories and rules of the persona
# ---------------------------------------------------------------------------
CATEGORIES = {
    ("GASTO", TransactionType.expense): [
        "Supermercados", "Restaurantes & Bares", "Luz", "Gas", "Agua", "Teléfono & Internet",
        "Comunidad", "Seguros", "IBI & Tasas", "Gasolina", "Peajes", "Parking",
        "Transporte público", "Amazon", "Compras Hogar", "Ropa & Calzado", "Salud & Farmacia",
        "Deporte", "Suscripciones", "Ocio & Cultura", "Viajes", "Regalos", "Delivery",
        "Peluquería", "Efectivo (cajero)", "Bizum enviado",
    ],
    ("INGRESO", TransactionType.income): [
        "Nómina", "Bizum recibido", "Devolución Hacienda", "Reembolsos", "Ventas Wallapop",
    ],
    ("SERV. BANCARIO", TransactionType.expense): [
        "Hipoteca", "Comisiones",
    ],
    ("INVERSION", TransactionType.investment): [
        "Aporte Broker", "Plan de Pensiones",
    ],
}

RULES = [  # (category, pattern[, match type]) — "contains" unless stated; issuer or concept
    ("Supermercados", "MERCADONA"), ("Supermercados", "EROSKI"), ("Supermercados", "LIDL"),
    ("Supermercados", "BM SUPERMERCADOS"), ("Supermercados", "CARREFOUR"),
    ("Luz", "IBERDROLA"), ("Gas", "TOTALENERGIES"), ("Agua", "CONSORCIO DE AGUAS"),
    ("Teléfono & Internet", "EUSKALTEL"), ("Comunidad", "COMUNIDAD PROP"),
    ("Seguros", "SEGURCAIXA"), ("Seguros", "MAPFRE"), ("IBI & Tasas", "AYUNTAMIENTO DE BILBAO"),
    ("Gasolina", "REPSOL"), ("Gasolina", "PETRONOR"), ("Peajes", "INTERBIAK"),
    ("Peajes", "AUTOPISTA"), ("Parking", "TELPARK"), ("Parking", "PARKING"),
    ("Transporte público", "METRO BILBAO"), ("Transporte público", "BILBOBUS"),
    ("Transporte público", "RENFE"), ("Amazon", "AMAZON"), ("Compras Hogar", "IKEA"),
    ("Compras Hogar", "LEROY MERLIN"), ("Ropa & Calzado", "ZARA"), ("Ropa & Calzado", "DECATHLON"),
    ("Ropa & Calzado", "PRIMARK"), ("Salud & Farmacia", "FARMACIA"), ("Deporte", "BILBAO KIROLAK"),
    ("Suscripciones", "NETFLIX"), ("Suscripciones", "SPOTIFY"), ("Suscripciones", "APPLE.COM"),
    ("Ocio & Cultura", "CINES"), ("Ocio & Cultura", "ATHLETIC CLUB"), ("Ocio & Cultura", "MUSEO"),
    ("Viajes", "VUELING"), ("Viajes", "BOOKING.COM"), ("Viajes", "RENFE VIAJEROS"),
    ("Delivery", "GLOVO"), ("Delivery", "JUST EAT"), ("Peluquería", "PELUQUERIA"),
    ("Efectivo (cajero)", "REINTEGRO"), ("Bizum enviado", "BIZUM A FAVOR"),
    ("Nómina", "TALLERES ARRIGORRIAGA"), ("Bizum recibido", "BIZUM DE"),
    ("Devolución Hacienda", "AEAT"), ("Ventas Wallapop", "WALLAPOP"),
    ("Hipoteca", "PRESTAMO 9620"), ("Comisiones", "COMISION"),
    ("Aporte Broker", "FLATEXDEGIRO"), ("Plan de Pensiones", "PLAN PENSIONES"),
    ("Restaurantes & Bares", "TABERNA"), ("Restaurantes & Bares", "RESTAURANTE"),
    ("Restaurantes & Bares", "SIDRERIA"), ("Restaurantes & Bares", "TXOKO"),
    ("Restaurantes & Bares", "CAFE IRU"), ("Restaurantes & Bares", "TELEPIZZA"),
    ("Restaurantes & Bares", "LA VIÑA"),
    ("Restaurantes & Bares", r"\bBAR\b", MatchType.regex),     # BAR EL TILO, not BARAKALDO
]
# More specific rules first (RENFE VIAJEROS is a trip, not a commute).
RULE_PRIORITY = {"RENFE VIAJEROS": 10, "PRESTAMO 9620": 10}


# ---------------------------------------------------------------------------
# Bank movements generator
# ---------------------------------------------------------------------------
@dataclass
class Movement:
    day: date
    amount: float
    col14: str
    details: tuple = ()
    comun: str = C_CARD


def _card(day: date, merchant: str, amount: float) -> Movement:
    """A card purchase, with the date prefix CaixaBank writes in column 14."""
    return Movement(day, -round(amount, 2), f"Fecha de operación: {day:%d-%m-%Y}   {merchant}",
                    ("COMPRA TARJETA 4B ****8812",), C_CARD)


def _bill(day: date, issuer: str, amount: float, *details: str) -> Movement:
    return Movement(day, -round(amount, 2), issuer, details or ("RECIBO",), C_BILL)


def _month_days(year: int, month: int) -> list[date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return [first + timedelta(days=i) for i in range((nxt - first).days)]


def _workday(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def generate_movements(rng: random.Random) -> list[Movement]:
    out: list[Movement] = []
    supermarkets = ["MERCADONA DEUSTO", "EROSKI CITY INDAUTXU", "LIDL ZORROTZA", "BM SUPERMERCADOS ABANDO",
                    "CARREFOUR EXPRESS SAN MAMES"]
    restaurants = ["BAR EL TILO", "TABERNA TXIKITO", "RESTAURANTE KASKAGORRI", "LA VIÑA DEL ENSANCHE",
                   "GURE TXOKO", "CAFE IRUÑA", "BAR BASAJAUN", "SIDRERIA ARRIZABALAGA", "TELEPIZZA DEUSTO"]
    fuel = ["REPSOL E.S. ZORROTZAURRE", "PETRONOR E.S. ARTXANDA", "PETRONOR E.S. LEIOA"]
    no_rule = ["FERRETERIA OTXOA", "LIBRERIA CAMARA", "FLORISTERIA LORATEGI", "TINTORERIA ARGIA",
               "KIOSKO MOYUA", "TPV ESTANCO ERCILLA"]
    friends = ["MIKEL ETXEBARRIA", "ANE GOIKOETXEA", "IKER LARRAÑAGA", "LEIRE ZUBIZARRETA", "UNAI ORTIZ"]

    day = START
    for year, month in sorted({(d.year, d.month) for d in
                               (START + timedelta(days=i) for i in range((END - START).days + 1))}):
        days = [d for d in _month_days(year, month) if START <= d <= END]
        winter = month in (11, 12, 1, 2, 3)
        mname = MONTHS_ES[month - 1]

        def on(dom: int) -> date | None:
            d = date(year, month, min(dom, days[-1].day if days else dom))
            return d if START <= d <= END else None

        # Payroll (with a 3 % raise in 2026), two extra payments a year.
        salary = 2_642.18 if year == 2025 else 2_721.44
        if (d := on(28)) is not None:
            d = _workday(d)
            out.append(Movement(d, round(salary + rng.uniform(-12, 12), 2), "TALLERES ARRIGORRIAGA INGENIERIA SL",
                                ("NOMINA", mname), C_PAYROLL))
            if month in (6, 12):
                out.append(Movement(d, round(salary * 0.91, 2), "TALLERES ARRIGORRIAGA INGENIERIA SL",
                                    ("PAGA EXTRA", mname), C_PAYROLL))

        # Fixed bills.
        if (d := on(1)) is not None:
            out.append(Movement(d + timedelta(days=(7 - d.weekday()) % 7 if d.weekday() >= 5 else 0), -742.18, "PRESTAMO 9620.311.4567891-27",
                                ("CUOTA PRESTAMO HIPOTECARIO",), C_LOAN))
        if (d := on(4)) is not None:
            out.append(_bill(d, "COMUNIDAD PROP. LICENCIADO POZA 47", 68.00, "CUOTA", mname))
        if (d := on(7)) is not None:
            out.append(_bill(d, "EUSKALTEL SA", 52.90 if year == 2025 else 54.90, "FIBRA+MOVIL", mname))
        if (d := on(9)) is not None:
            kwh = rng.uniform(78, 98) if winter else rng.uniform(44, 62)
            out.append(_bill(d, "IBERDROLA CLIENTES SAU", kwh, "RECIBO LUZ"))
        if month % 2 == 0 and (d := on(14)) is not None:
            out.append(_bill(d, "TOTALENERGIES CLIENTES SAU", rng.uniform(88, 118) if winter else rng.uniform(31, 42),
                             "RECIBO GAS"))
        if month in (2, 5, 8, 11) and (d := on(18)) is not None:
            out.append(_bill(d, "CONSORCIO DE AGUAS BILBAO BIZKAIA", rng.uniform(34, 44), "AGUA", "TRIMESTRE"))
        if (d := on(2)) is not None:
            out.append(_bill(d, "BILBAO KIROLAK", 32.50, "ABONO MENSUAL"))
        if (d := on(11)) is not None:
            out.append(_card(d, "NETFLIX.COM", 12.99 if year == 2025 else 13.99))
        if (d := on(15)) is not None:
            out.append(_card(d, "SPOTIFY P2C4A1B3E7", 11.99))
        if (d := on(20)) is not None:
            out.append(_card(d, "APPLE.COM/BILL", 2.99))

        # Saving: monthly transfer to the broker, pension plan in December.
        if (d := on(3)) is not None:
            out.append(Movement(_workday(d), -400.00, "TRANSFERENCIA A FLATEXDEGIRO BANK AG",
                                ("APORTACION MENSUAL",), C_TRANSFER))
        if month == 12 and (d := on(19)) is not None:
            out.append(Movement(d, -1_500.00, "APORTACION PLAN PENSIONES CAIXABANK", ("PP DESTINO",), C_TRANSFER))

        # Yearly / seasonal items.
        yearly = {
            (3, 10): _bill(date(year, 3, 10), "SEGURCAIXA ADESLAS SA", 312.40, "SEGURO HOGAR"),
            (11, 6): _bill(date(year, 11, 6), "MAPFRE ESPAÑA SA", 418.75, "SEGURO AUTO"),
            (6, 20): _bill(date(year, 6, 20), "AYUNTAMIENTO DE BILBAO", 389.12, "IBI"),
            (7, 2): _card(date(year, 7, 2), "ATHLETIC CLUB TAQUILLAS", 432.00),
            (1, 15): Movement(date(year, 1, 15), -36.00, "COMISION MANTENIMIENTO TARJETA", ("ANUAL",), C_FEE),
            (4, 2): _card(date(year, 4, 2), "AMAZON PRIME ES", 49.90),
        }
        for (m, dom), mv in yearly.items():
            if m == month and START <= date(year, m, dom) <= END:
                out.append(mv)
        if month == 7 and year in (2025, 2026) and (d := on(24)) is not None:
            out.append(Movement(d, 412.37 if year == 2025 else 286.15, "AEAT DEVOLUCION RENTA",
                                (f"EJERCICIO {year - 1}",), C_TRANSFER))
        if month == 8 and (d := on(5)) is not None:     # summer holidays
            out += [_card(d - timedelta(days=30), "VUELING AIRLINES", rng.uniform(170, 240)),
                    _card(d - timedelta(days=28), "BOOKING.COM HOTEL", rng.uniform(380, 520)),
                    _card(d + timedelta(days=3), "RESTAURANTE O PAZO", rng.uniform(55, 80)),
                    _card(d + timedelta(days=6), "MUSEO DO POVO GALEGO", 12.00)]
        if month == 4 and (d := on(16)) is not None:    # Easter
            out += [_card(d, "RENFE VIAJEROS SA", rng.uniform(70, 95)),
                    _card(d + timedelta(days=2), "BOOKING.COM HOTEL", rng.uniform(190, 260))]
        if month == 12 and (d := on(16)) is not None:   # Christmas presents
            out += [_card(d, "ZARA BILBAO GRAN VIA", rng.uniform(80, 130)),
                    _card(d + timedelta(days=2), "AMAZON EU SARL", rng.uniform(60, 110)),
                    _card(d + timedelta(days=4), "LIBRERIA CAMARA", rng.uniform(25, 45))]

        # Variable day-to-day spending.
        for d in days:
            wk = d.weekday()
            if wk in (1, 5) or rng.random() < 0.08:
                out.append(_card(d, rng.choice(supermarkets), rng.uniform(18, 96) if wk == 5 else rng.uniform(9, 42)))
            if (wk >= 4 and rng.random() < 0.45) or rng.random() < 0.06:
                out.append(_card(d, rng.choice(restaurants), rng.uniform(8, 58)))
            if rng.random() < 0.095:
                out.append(_card(d, rng.choice(fuel), rng.uniform(42, 68)))
            if wk < 5 and rng.random() < 0.06:
                out.append(_card(d, "INTERBIAK AUTOPISTA", rng.choice([1.62, 2.05, 3.10])))
            if rng.random() < 0.07:
                out.append(_card(d, rng.choice(["TELPARK", "PARKING INDAUTXU"]), rng.uniform(1.8, 9.5)))
            if wk < 5 and rng.random() < 0.08:
                out.append(_card(d, rng.choice(["METRO BILBAO", "BILBOBUS"]), rng.choice([1.70, 2.10, 10.00])))
            if rng.random() < 0.055:
                out.append(_card(d, "AMAZON EU SARL", rng.uniform(9, 85)))
            if rng.random() < 0.035:
                out.append(_card(d, rng.choice(["GLOVO APP", "JUST EAT SPAIN"]), rng.uniform(16, 34)))
            if rng.random() < 0.025:
                out.append(_card(d, "FARMACIA LDA. MENDIZABAL", rng.uniform(5, 32)))
            if rng.random() < 0.018:
                out.append(_card(d, rng.choice(["DECATHLON BARAKALDO", "PRIMARK BILBAO", "ZARA BILBAO GRAN VIA"]),
                                 rng.uniform(19, 89)))
            if rng.random() < 0.012:
                out.append(_card(d, rng.choice(["IKEA BARAKALDO", "LEROY MERLIN BARAKALDO"]), rng.uniform(15, 140)))
            if rng.random() < 0.02:
                out.append(_card(d, rng.choice(["CINES GOLEM ALHONDIGA", "MUSEO GUGGENHEIM BILBAO"]),
                                 rng.choice([7.90, 9.50, 16.00, 18.00])))
            if rng.random() < 0.02:
                out.append(_card(d, rng.choice(no_rule), rng.uniform(4, 38)))
            if rng.random() < 0.03:
                out.append(Movement(d, round(rng.choice([12, 15, 18.5, 22, 25, 30]) + rng.choice([0, 0.5]), 2),
                                    f"BIZUM DE {rng.choice(friends)}", ("CENA", ), C_BIZUM))
            if rng.random() < 0.015:
                out.append(Movement(d, -float(rng.choice([10, 15, 20, 25])), f"BIZUM A FAVOR DE {rng.choice(friends)}",
                                    ("REGALO",), C_BIZUM))
        if (d := on(rng.randint(8, 22))) is not None:
            out.append(Movement(d, -float(rng.choice([50, 60, 100])), "REINTEGRO CAJERO CAIXABANK",
                                ("CAJERO 0412 BILBAO",), C_ATM))
        if rng.random() < 0.5 and (d := on(rng.randint(5, 25))) is not None:
            out.append(_card(d, "PELUQUERIA IZASKUN", rng.choice([16.0, 18.0, 22.0])))
        if rng.random() < 0.15 and (d := on(rng.randint(5, 25))) is not None:
            out.append(Movement(d, round(rng.uniform(15, 120), 2), "WALLAPOP SL", ("VENTA ARTICULO",), C_TRANSFER))

    out = [m for m in out if START <= m.day <= END]
    out.sort(key=lambda m: (m.day, -m.amount))
    # The duplicate detector keys on (date, concepto común, amount): keep the
    # generated data free of accidental "duplicates".
    seen = set()
    for m in out:
        m.amount = round(m.amount, 2)
        while (m.day, m.comun, m.amount) in seen:
            m.amount = round(m.amount - 0.01 if m.amount < 0 else m.amount + 0.01, 2)
        seen.add((m.day, m.comun, m.amount))
    return out


# ---------------------------------------------------------------------------
# CaixaBank statement writer
# ---------------------------------------------------------------------------
def _row(m: Movement, balance: float) -> list:
    row = [None] * 24
    row[1], row[2], row[3] = IBAN, "1234", "EUR"
    row[4] = row[5] = m.day.strftime("%d/%m/%Y")
    if m.amount >= 0:
        row[6] = m.amount
    else:
        row[7] = -m.amount
    if balance >= 0:
        row[8] = round(balance, 2)
    else:
        row[9] = round(balance, 2)
    row[10], row[11], row[12] = m.comun, "000", "000000000000"
    row[13] = f"{zlib.crc32(f'{m.day}{m.col14}{m.amount}'.encode()) % 10**10:010d}"
    row[14] = m.col14
    for offset, text in enumerate(m.details):
        row[15 + offset] = text
    return row


def write_statement(path: Path, rows: list[tuple[Movement, float]], first: date, last: date) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Movimientos"
    ws.append([None] * 24)
    ws.append([None, f"MOVIMIENTOS DESDE {first:%d/%m/%Y} HASTA {last:%d/%m/%Y}"] + [None] * 22)
    ws.append([None] * 24)
    header = [None, "IBAN", "Oficina", "Divisa", "F. Operación", "F. Valor", "Ingreso (+)", "Gasto (-)",
              "Saldo (+)", "Saldo (-)", "Concepto común", "Concepto propio", "Referencia 1", "Referencia 2",
              "Concepto complementario 1"] + [f"Concepto complementario {i}" for i in range(2, 10)] + [None]
    ws.append(header)
    for m, balance in reversed(rows):            # CaixaBank lists the newest movement first
        ws.append(_row(m, balance))
    wb.save(path)


# ---------------------------------------------------------------------------
# Investments
# ---------------------------------------------------------------------------
PORTFOLIO = [  # symbol, name, type, currency, exchange, sector, country, isin
    ("VWCE.DE", "Vanguard FTSE All-World UCITS ETF (Acc)", AssetType.etf, "EUR", "XETRA", None, "Irlanda", "IE00BK5BQT80"),
    ("ITX.MC", "Industria de Diseño Textil, S.A.", AssetType.stock, "EUR", "BME", "Consumer Cyclical", "España", "ES0148396007"),
    ("IBE.MC", "Iberdrola, S.A.", AssetType.stock, "EUR", "BME", "Utilities", "España", "ES0144580Y14"),
    ("SAN.MC", "Banco Santander, S.A.", AssetType.stock, "EUR", "BME", "Financial Services", "España", "ES0113900J37"),
    ("MSFT", "Microsoft Corporation", AssetType.stock, "USD", "NasdaqGS", "Technology", "United States", "US5949181045"),
]
# One-off operations: (date, symbol, kind, quantity)
ONE_OFF = [
    (date(2025, 1, 21), "SAN.MC", InvestmentKind.buy, 300),
    (date(2025, 2, 12), "IBE.MC", InvestmentKind.buy, 60),
    (date(2025, 3, 18), "ITX.MC", InvestmentKind.buy, 20),
    (date(2025, 6, 10), "MSFT", InvestmentKind.buy, 5),
    (date(2025, 11, 13), "SAN.MC", InvestmentKind.sell, 150),
    (date(2026, 3, 5), "IBE.MC", InvestmentKind.buy, 25),
]
BROKER = "DEGIRO"
FEE_SPAIN, FEE_US, FEE_ETF = 3.90, 2.00, 1.00


def _synthetic_closes(start: date, end: date, first: float, rng: random.Random, drift: float) -> list:
    rows, close, d = [], first, start
    while d <= end:
        if d.weekday() < 5:
            close *= math.exp(rng.gauss(drift / 252, 0.011))
            rows.append((d, round(close, 3)))
        d += timedelta(days=1)
    return rows


def build_investments(session, user_id: int, rng: random.Random, offline: bool) -> str:
    from app.investments.market_data import YahooProvider, MarketDataError
    from app.investments.prices import PRICE_SOURCE_MANUAL, PRICE_SOURCE_YAHOO, refresh_market_data, set_manual_price

    provider = YahooProvider()
    closes: dict[str, list] = {}
    fx: list = []
    mode = "offline"
    if not offline:
        try:
            symbols = [p[0] for p in PORTFOLIO] + ["EURUSD=X"]
            closes = provider.daily_closes(symbols, start=START - timedelta(days=10), end=END)
            fx = closes.pop("EURUSD=X", [])
            if all(closes.get(p[0]) for p in PORTFOLIO) and fx:
                mode = "yahoo"
        except MarketDataError as exc:
            print(f"  Yahoo Finance no disponible ({exc}); se usan precios sintéticos.")
    if mode == "offline":
        seeds = {"VWCE.DE": (118.0, 0.10), "ITX.MC": (49.0, 0.04), "IBE.MC": (13.4, 0.18),
                 "SAN.MC": (4.5, 0.35), "MSFT": (420.0, 0.08)}
        closes = {s: _synthetic_closes(START - timedelta(days=10), END, p, rng, dr) for s, (p, dr) in seeds.items()}
        fx = _synthetic_closes(START - timedelta(days=10), END, 1.04, rng, 0.05)

    def close_on(series: list, day: date) -> float:
        best = None
        for d, c in series:
            if d > day:
                break
            best = c
        return best if best is not None else series[0][1]

    securities, created = {}, set()
    for symbol, name, kind, cur, exch, sector, country, isin in PORTFOLIO:
        sec = session.query(Security).filter(Security.symbol == symbol).one_or_none()
        if sec is None:
            sec = Security(symbol=symbol, name=name, asset_type=kind, currency=cur, exchange=exch, sector=sector,
                           country=country, isin=isin,
                           price_source=PRICE_SOURCE_YAHOO if mode == "yahoo" else PRICE_SOURCE_MANUAL)
            session.add(sec)
            created.add(symbol)
        securities[symbol] = sec
    session.flush()

    ops: list[InvestmentTransaction] = []
    held: dict[str, float] = {}

    def add(day: date, symbol: str, kind: InvestmentKind, qty: float, ref: str, **extra) -> None:
        sec = securities[symbol]
        price = round(close_on(closes[symbol], day) * rng.uniform(0.997, 1.003), 3)
        rate = close_on(fx, day) if sec.currency == "USD" else 1.0
        gross = qty * price / rate
        fee = FEE_US if sec.currency == "USD" else FEE_ETF if sec.asset_type == AssetType.etf else FEE_SPAIN
        amount = gross + fee if kind == InvestmentKind.buy else gross - fee
        held[symbol] = held.get(symbol, 0) + (qty if kind == InvestmentKind.buy else -qty)
        ops.append(InvestmentTransaction(
            user_id=user_id, security_id=sec.id, broker=BROKER, kind=kind, trade_date=day, quantity=qty,
            price=price, currency=sec.currency, amount_eur=round(amount, 2), fees_eur=fee, broker_ref=ref, **extra))

    # Monthly ETF purchase with the 400 € transferred on the 3rd.
    d, n = START, 0
    while d <= END:
        day = date(d.year, d.month, 6)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        if day <= END:
            price = close_on(closes["VWCE.DE"], day)
            add(day, "VWCE.DE", InvestmentKind.buy, max(1, int((400 - FEE_ETF) // price)), f"DEMO-ETF-{n:03d}")
        n += 1
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    for i, (day, symbol, kind, qty) in enumerate(ONE_OFF):
        add(day, symbol, kind, qty, f"DEMO-ORD-{i:03d}")

    # Dividends actually paid by those companies while held (19 % Spanish
    # withholding; 15 % US withholding for MSFT).
    dividends = {}
    if mode == "yahoo":
        for symbol in ("ITX.MC", "IBE.MC", "SAN.MC", "MSFT"):
            try:
                dividends[symbol] = [(d, v) for d, v in provider.corporate_actions(symbol).dividends
                                     if START <= d <= END]
            except MarketDataError:
                dividends[symbol] = []
    else:
        dividends = {"ITX.MC": [(date(2025, 5, 2), 0.77), (date(2025, 11, 4), 0.77), (date(2026, 5, 4), 0.84)],
                     "IBE.MC": [(date(2025, 7, 30), 0.404), (date(2026, 1, 29), 0.231), (date(2026, 7, 30), 0.43)],
                     "SAN.MC": [(date(2025, 5, 2), 0.1), (date(2025, 11, 3), 0.115), (date(2026, 5, 4), 0.11)],
                     "MSFT": [(date(2025, 9, 11), 0.83), (date(2025, 12, 11), 0.91), (date(2026, 3, 12), 0.91)]}
    for symbol, events in dividends.items():
        sec = securities[symbol]
        for k, (day, per_share) in enumerate(events):
            qty = sum(o.quantity * (1 if o.kind == InvestmentKind.buy else -1) for o in ops
                      if o.security_id == sec.id and o.trade_date < day and o.kind in (InvestmentKind.buy, InvestmentKind.sell))
            if qty <= 0:
                continue
            rate = close_on(fx, day) if sec.currency == "USD" else 1.0
            gross = qty * per_share / rate
            tax = gross * (0.15 if sec.currency == "USD" else 0.19)
            ops.append(InvestmentTransaction(
                user_id=user_id, security_id=sec.id, broker=BROKER, kind=InvestmentKind.dividend, trade_date=day,
                quantity=qty, price=per_share, currency=sec.currency, amount_eur=round(gross - tax, 2),
                tax_withheld_eur=round(tax, 2), broker_ref=f"DEMO-DIV-{symbol}-{k}"))

    session.add_all(ops)
    session.commit()

    if mode == "yahoo":
        from app.investments.benchmarks import BENCHMARKS
        from app.investments.performance import ensure_benchmark

        for key in BENCHMARKS:      # so "Comparar con MSCI World / S&P 500 / IBEX" has data
            ensure_benchmark(session, key)
        result = refresh_market_data(session, provider, today=END)
        if result.error:
            print(f"  Aviso al descargar cotizaciones: {result.error}")
    else:
        for symbol, series in closes.items():
            if symbol not in created:
                continue
            for d, c in series:
                set_manual_price(session, securities[symbol].id, d, c)
    return f"{len(ops)} operaciones de inversión ({'cotizaciones reales de Yahoo Finance' if mode == 'yahoo' else 'precios sintéticos'})"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def populate(session, username: str, password: str, statements_dir: Path, offline: bool = False):
    """Create the user with the persona's account, categories, rules,
    imported statements and investments. Prints a summary; returns the user."""
    statements_dir.mkdir(parents=True, exist_ok=True)
    for old in statements_dir.glob("CaixaBank_Movimientos_*.xlsx"):
        old.unlink()
    rng = random.Random(20251)
    user = create_user(session, username, password)
    account = Account(user_id=user.id, account_name="CaixaBank Nómina", bank="CaixaBank", iban_last4=IBAN[-4:])
    session.add(account)
    session.commit()

    by_name: dict[str, Category] = {}
    for (type_name, tx_type), names in CATEGORIES.items():
        parent = create_category(session, user.id, type_name, CategoryLevel.type, transaction_type=tx_type)
        for name in names:
            by_name[name] = create_category(session, user.id, name, CategoryLevel.category, parent_id=parent.id)
    for name, pattern, *match in RULES:
        create_rule(session, by_name[name].id, pattern, match[0] if match else MatchType.contains,
                    priority=RULE_PRIORITY.get(pattern, 100), user_id=user.id)
    print(f"  {len(by_name)} categorías y {len(RULES)} reglas")

    movements = generate_movements(rng)
    balance, with_balance = OPENING_BALANCE, []
    for m in movements:
        balance = round(balance + m.amount, 2)
        with_balance.append((m, balance))

    # One statement per month, imported through the real pipeline.
    months = sorted({(m.day.year, m.day.month) for m in movements})
    imported = skipped = statements = 0
    for year, month in months:
        rows = [(m, b) for m, b in with_balance
                if (m.day.year, m.day.month) == (year, month) and m.day <= LAST_IMPORTED_DAY]
        if not rows:
            continue
        days = [d for d in _month_days(year, month) if d <= LAST_IMPORTED_DAY]
        path = statements_dir / f"CaixaBank_Movimientos_{year}-{month:02d}.xlsx"
        write_statement(path, rows, days[0], days[-1])
        statements += 1
        preview = prepare_import(path, session, user.id, account.id)
        result = run_import(session, user.id, preview, account.id, {})
        imported += len(result.summary.imported_ids)
        skipped += len(preview.df) - len(result.summary.imported_ids)
        # Realistic import history: imported a few days after the month ended.
        batch = session.query(ImportBatch).order_by(ImportBatch.id.desc()).first()
        nxt = days[-1] + timedelta(days=rng.randint(2, 5))
        batch.import_timestamp = datetime(nxt.year, nxt.month, nxt.day, rng.randint(19, 22), rng.randint(0, 59))
        session.commit()
    print(f"  {imported} movimientos importados en {statements} extractos"
          + (f" (¡{skipped} omitidos!)" if skipped else ""))

    pending = [(m, b) for m, b in with_balance if m.day >= PENDING_FROM]
    pending_path = statements_dir / "CaixaBank_Movimientos_2026-10_PENDIENTE_DE_IMPORTAR.xlsx"
    write_statement(pending_path, pending, PENDING_FROM, END)
    print(f"  Extracto pendiente de importar: {pending_path.name} ({len(pending)} filas, solapa con la última importación)")

    # A couple of manual touches a real user makes.
    from app.categorization.category_manager import assign_user_transaction_category
    flor = session.query(Transaction).filter(Transaction.user_id == user.id,
                                             Transaction.issuer_name.like("%FLORISTERIA%")).all()
    for t in flor:
        assign_user_transaction_category(session, user.id, t.id, by_name["Regalos"].id)
    uncategorized = session.query(Transaction).filter(Transaction.user_id == user.id,
                                                      Transaction.category_id.is_(None)).count()
    print(f"  {uncategorized} movimientos sin categoría (para mostrar la categorización manual)")

    print("  " + build_investments(session, user.id, rng, offline))
    user.last_login = datetime(2026, 10, 12, 21, 4)
    session.commit()
    return user
