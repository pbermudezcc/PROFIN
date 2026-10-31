"""
app/analytics/forecast.py
=========================
FAMILYFIN — Previsiones: fixed-spending baseline, next-month forecast and
the money each member moves to another (aportes to the joint account).

Qt-free; shared by the desktop ForecastView and the web /prevision page.

How a movement counts
---------------------
* Its nature comes from models.effective_nature: the category's Fijo /
  Traspaso mark unless the movement overrides Fijo/Variable by hand.
* Investment categories (dashboard_summary.investment_category_ids) are left
  out. Traspaso movements are left out of spending and income and reported
  apart (`transfers`): an aporte is neither a bill nor a salary, and counting
  it on both sides would double the family totals.
* A categorized movement is an expense or an income by its category's type
  (GASTO / INGRESO…), netting refunds: a refund into «Luz» lowers Luz. An
  uncategorized one goes by its sign.

Windows
-------
* The baseline averages the last 12 *complete* months (the current month is
  never in it) that have data; fewer than 12 = provisional, fewer than
  MIN_MONTHS = not enough data.
* Bills that come every 2, 3, 6 or 12 months are spread over the window
  (an annual 600 € insurance is 50 €/month of baseline) but the forecast
  only shows them in the month they are due.

Bill series
-----------
Fixed movements are grouped by category + normalised issuer/concept. The
frequency is the median gap between charges; the expected amount the median
of the last 3 (the last one for annual bills). A series with no charge for
1.5 periods is treated as finished and left out of the forecast.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy.orm import Session

from app.analytics.dashboard_summary import investment_category_ids
from app.analytics.transaction_search import fold_text
from app.categorization.rules_engine import shares_global_rules
from app.db.models import (
    CATEGORY_NATURE_FIXED,
    CATEGORY_NATURE_TRANSFER,
    Category,
    Transaction,
    TransactionType,
    User,
    effective_nature,
)

logger = logging.getLogger(__name__)

WINDOW_MONTHS = 12          # baseline
VARIABLE_MONTHS = 6         # median of the variable spending / income
HISTORY_MONTHS = 25         # read for the series (an annual bill needs two charges)
MIN_MONTHS = 3
ABOVE_USUAL = 0.20          # a bill this much above its usual amount is flagged
ABOVE_USUAL_MIN_EUR = 5.0

PERIOD_LABELS = {1: "Mensual", 2: "Bimestral", 3: "Trimestral", 4: "Cuatrimestral",
                 6: "Semestral", 12: "Anual"}
MONTH_NAMES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
               "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
UNCATEGORIZED_LABEL = "Sin categorizar"

STATUS_CHARGED = "cargado"
STATUS_PENDING = "pendiente"

Month = Tuple[int, int]


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class Series:
    """One recurring fixed bill (or fixed income) of one user."""
    label:          str                 # «IBERDROLA» (as the bank writes it)
    category:       str
    kind:           str                 # "expense" | "income"
    period:         Optional[int]       # months between charges; None = irregular
    usual:          float               # expected amount per charge (positive)
    monthly:        float               # average per month over the window
    last_date:      date
    next_due:       Optional[date]
    active:         bool
    charges:        int

    @property
    def period_label(self) -> str:
        return PERIOD_LABELS.get(self.period, "Irregular")


@dataclass
class ForecastItem:
    """A line of «Este mes» or «Mes próximo»."""
    label:     str
    category:  str
    kind:      str                      # "expense" | "income"
    expected:  float
    period:    Optional[int] = None
    status:    Optional[str] = None     # STATUS_* (this month only)
    actual:    Optional[float] = None
    day:       Optional[int] = None     # approximate day of the month
    above_pct: Optional[float] = None   # % above the usual amount, when flagged

    @property
    def period_label(self) -> str:
        return PERIOD_LABELS.get(self.period, "Irregular")


@dataclass
class MonthSplit:
    year:     int
    month:    int
    fixed:    float                     # gastos fijos
    variable: float                     # gastos variables
    income:   float = 0.0               # ingresos (fijos + variables, sin traspasos)

    @property
    def label(self) -> str:
        return f"{MONTH_NAMES[self.month][:3]} {str(self.year)[2:]}"


@dataclass
class TransferLine:
    """Traspaso movements of one category of one user, per month."""
    owner:      str                     # user name the data comes from
    owner_id:   int
    category:   str
    peer:       Optional[str]           # «Con» member (None = not linked yet)
    peer_id:    Optional[int]
    direction:  str                     # "in" (received) | "out" (sent)
    by_month:   Dict[Month, float] = field(default_factory=dict)   # positive amounts


@dataclass
class Contribution:
    """Money one member sent to another, per month (Aportaciones table)."""
    sender:      str
    receiver:    str
    this_month:  float
    last_month:  float
    average:     float                  # over the window months
    total:       float                  # over the window months
    share_pct:   Optional[float]        # of everything the receiver got
    by_month:    Dict[Month, float]
    mismatches:  List[str] = field(default_factory=list)   # «sep 26: enviado 500 €, recibido 450 €»


@dataclass
class Forecast:
    today:              date
    months:             int                         # complete months in the window with data
    window:             List[Month]
    # Baseline (per month)
    fixed_baseline:     float = 0.0
    fixed_by_category:  List[Tuple[str, float]] = field(default_factory=list)
    series:             List[Series] = field(default_factory=list)
    variable_median:    float = 0.0
    variable_mean:      float = 0.0
    income_fixed:       float = 0.0                 # average fixed income per month
    income_variable:    float = 0.0                 # median variable income per month
    monthly_split:      List[MonthSplit] = field(default_factory=list)
    # This month / next month
    this_month:         List[ForecastItem] = field(default_factory=list)
    next_month:         List[ForecastItem] = field(default_factory=list)
    next_fixed:         float = 0.0
    next_other_fixed:   float = 0.0                 # irregular fixed bills (average)
    next_other_income:  float = 0.0                 # irregular fixed income (average)
    next_income:        float = 0.0                 # all expected income, without traspasos
    # Expected traspasos next month, one per member (own view only; empty in
    # Familia, where they cancel out): kind "transfer_in" / "transfer_out".
    next_transfers:     List[ForecastItem] = field(default_factory=list)
    # Transfers (traspaso) — outside every total above
    transfers:          List[TransferLine] = field(default_factory=list)
    transfers_in_avg:   float = 0.0
    transfers_out_avg:  float = 0.0
    contributions:      List[Contribution] = field(default_factory=list)
    uncategorized:      int = 0                     # in the window (count as variable)
    warnings:           List[str] = field(default_factory=list)
    members:            List[str] = field(default_factory=list)   # Familia only

    # ---- derived -------------------------------------------------------
    @property
    def enough_data(self) -> bool:
        return self.months >= MIN_MONTHS

    @property
    def provisional(self) -> bool:
        return self.months < WINDOW_MONTHS

    @property
    def next_month_key(self) -> Month:
        return _add_months((self.today.year, self.today.month), 1)

    @property
    def next_month_label(self) -> str:
        y, m = self.next_month_key
        return f"{MONTH_NAMES[m]} {y}"

    @property
    def this_month_label(self) -> str:
        return f"{MONTH_NAMES[self.today.month]} {self.today.year}"

    @property
    def next_expenses(self) -> float:
        return round(self.next_fixed + self.next_other_fixed + self.variable_median, 2)

    @property
    def next_saving(self) -> float:
        return round(self.next_income - self.next_expenses, 2)

    @property
    def baseline_total(self) -> float:
        """Average spending per month: fixed + variable (median)."""
        return round(self.fixed_baseline + self.variable_median, 2)

    @property
    def receives_transfers(self) -> bool:
        return self.transfers_in_avg > 0

    @property
    def required_contribution(self) -> float:
        """What has to come in each month for the account to cover itself:
        average spending minus its own (non-traspaso) income."""
        return round(max(0.0, self.baseline_total - self.income_fixed - self.income_variable), 2)

    @property
    def contribution_gap(self) -> float:
        """Average aportes received minus what is needed (negative = short)."""
        return round(self.transfers_in_avg - self.required_contribution, 2)

    @property
    def pending_this_month(self) -> float:
        """Bills still to be charged this month."""
        return round(sum(i.expected for i in self.this_month_expenses if i.status == STATUS_PENDING), 2)

    @property
    def pending_income_this_month(self) -> float:
        """Fixed income still to come in this month."""
        return round(sum(i.expected for i in self.this_month_income if i.status == STATUS_PENDING), 2)

    # ---- the two sides, never mixed ---------------------------------------
    @property
    def this_month_income(self) -> List[ForecastItem]:
        return [i for i in self.this_month if i.kind == "income"]

    @property
    def this_month_expenses(self) -> List[ForecastItem]:
        return [i for i in self.this_month if i.kind == "expense"]

    @property
    def next_month_income(self) -> List[ForecastItem]:
        return [i for i in self.next_month if i.kind == "income"]

    @property
    def next_month_expenses(self) -> List[ForecastItem]:
        return [i for i in self.next_month if i.kind == "expense"]

    @property
    def income_series(self) -> List[Series]:
        return [s for s in self.series if s.kind == "income"]

    @property
    def expense_series(self) -> List[Series]:
        return [s for s in self.series if s.kind == "expense"]

    @property
    def next_fixed_income(self) -> float:
        return round(sum(i.expected for i in self.next_month_income), 2)

    @property
    def next_transfers_in(self) -> List[ForecastItem]:
        return [i for i in self.next_transfers if i.kind == "transfer_in"]

    @property
    def next_transfers_out(self) -> List[ForecastItem]:
        return [i for i in self.next_transfers if i.kind == "transfer_out"]

    @property
    def next_income_with_transfers(self) -> float:
        return round(self.next_income + sum(i.expected for i in self.next_transfers_in), 2)

    @property
    def next_expenses_with_transfers(self) -> float:
        return round(self.next_expenses + sum(i.expected for i in self.next_transfers_out), 2)

    @property
    def next_saving_with_transfers(self) -> float:
        return round(self.next_income_with_transfers - self.next_expenses_with_transfers, 2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_months(month: Month, n: int) -> Month:
    index = month[0] * 12 + (month[1] - 1) + n
    return index // 12, index % 12 + 1


def _month_of(d: date) -> Month:
    return d.year, d.month


def _add_months_to_date(d: date, n: int) -> date:
    y, m = _add_months(_month_of(d), n)
    last_day = (date(*_add_months((y, m), 1), 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))


def _r(value: float) -> float:
    return round(value + 0.0, 2)


_NOISE = re.compile(r"[^a-z ]+")


def series_key(text: Optional[str]) -> str:
    """«IBERDROLA CLIENTES, S.A.U. 0012345» → «iberdrola clientes s»: no digits
    or punctuation (references and dates change every month), first 3 words."""
    words = _NOISE.sub(" ", fold_text(text or "").replace(".", "")).split()   # S.A.U. = SAU
    return " ".join(words[:3])


def _period_of(dates: Sequence[date]) -> Optional[int]:
    """Months between charges from the median gap in days, or None."""
    days = sorted(set(dates))
    if len(days) < 2:
        return None
    gap = median((b - a).days for a, b in zip(days, days[1:]))
    for months in PERIOD_LABELS:
        expected = months * 30.44
        if abs(gap - expected) <= max(6.0, 0.15 * expected):
            return months
    return None


@dataclass
class _Cat:
    name:   str
    nature: Optional[str]
    kind:   Optional[str]        # "expense" | "income" | "investment" | None (by sign)
    peer:   Optional[int]


def _categories(session: Session, user_id: int) -> Dict[int, _Cat]:
    rows = {
        c.id: c for c in session.query(Category).filter(Category.user_id == user_id)
    }
    found: Dict[int, _Cat] = {}
    for cid, cat in rows.items():
        node, seen, kind = cat, set(), None
        while node is not None and node.id not in seen:     # nearest type above
            seen.add(node.id)
            if node.transaction_type is not None:
                kind = node.transaction_type.value
                break
            node = rows.get(node.parent_id)
        if kind not in (TransactionType.expense.value, TransactionType.income.value,
                        TransactionType.investment.value):
            kind = None
        found[cid] = _Cat(cat.name, cat.nature, kind, cat.transfer_peer_user_id)
    return found


# ---------------------------------------------------------------------------
# One user
# ---------------------------------------------------------------------------

def user_forecast(session: Session, user_id: int, today: Optional[date] = None) -> Forecast:
    today = today or date.today()
    current = _month_of(today)
    cats = _categories(session, user_id)
    invest_ids = investment_category_ids(session, user_id)
    names = dict(session.query(User.id, User.username))

    history_start = date(*_add_months(current, -HISTORY_MONTHS), 1)
    txns = (
        session.query(Transaction)
        .filter(Transaction.user_id == user_id, Transaction.transaction_date >= history_start,
                Transaction.transaction_date <= today)
        .all()
    )
    first = (session.query(Transaction.transaction_date)
             .filter(Transaction.user_id == user_id)
             .order_by(Transaction.transaction_date).limit(1).scalar())
    window = [m for m in (_add_months(current, -i) for i in range(WINDOW_MONTHS, 0, -1))
              if first is not None and m >= _month_of(first)]
    window_set = set(window)
    fc = Forecast(today=today, months=len(window), window=window)

    fixed_by_month: Dict[Month, float] = defaultdict(float)
    variable_by_month: Dict[Month, float] = defaultdict(float)
    var_income_by_month: Dict[Month, float] = defaultdict(float)
    fixed_income_by_month: Dict[Month, float] = defaultdict(float)
    fixed_by_cat: Dict[str, float] = defaultdict(float)
    # (kind, category id, key) → [(date, amount, text)]
    charges: Dict[Tuple[str, Optional[int], str], List[Tuple[date, float, str]]] = defaultdict(list)
    transfers: Dict[int, TransferLine] = {}

    for t in txns:
        if t.category_id in invest_ids:
            continue
        cat = cats.get(t.category_id)
        month = _month_of(t.transaction_date)
        nature = effective_nature(t.fixed_override, cat.nature if cat else None)
        if nature == CATEGORY_NATURE_TRANSFER:
            line = transfers.get(t.category_id)
            if line is None:
                line = transfers[t.category_id] = TransferLine(
                    owner=names.get(user_id, ""), owner_id=user_id, category=cat.name,
                    peer=names.get(cat.peer), peer_id=cat.peer if cat.peer in names else None,
                    direction="in" if cat.kind == "income" or (cat.kind is None and t.amount > 0) else "out")
            amount = t.amount if line.direction == "in" else -t.amount
            line.by_month[month] = line.by_month.get(month, 0.0) + amount
            continue

        kind = cat.kind if cat and cat.kind in ("expense", "income") else (
            "income" if t.amount > 0 else "expense")
        value = -t.amount if kind == "expense" else t.amount       # refunds net out
        if t.category_id is None and month in window_set:
            fc.uncategorized += 1
        fixed = nature == CATEGORY_NATURE_FIXED
        if fixed and value > 0:                       # a refund is not a charge of the series
            text = t.issuer_name or t.concept or (cat.name if cat else "")
            charges[(kind, t.category_id, series_key(text))].append((t.transaction_date, value, text))
        if month not in window_set:
            continue                                  # the current month is never averaged
        if kind == "expense":
            if fixed:
                fixed_by_month[month] += value
                fixed_by_cat[cat.name if cat else UNCATEGORIZED_LABEL] += value
            else:
                variable_by_month[month] += value
        elif fixed:
            fixed_income_by_month[month] += value
        else:
            var_income_by_month[month] += value

    months = max(1, fc.months)
    fc.fixed_baseline = _r(sum(fixed_by_month.values()) / months)
    fc.fixed_by_category = sorted(((n, _r(v / months)) for n, v in fixed_by_cat.items() if abs(v) >= 0.005),
                                  key=lambda item: -item[1])
    fc.income_fixed = _r(sum(fixed_income_by_month.values()) / months)
    recent = window[-VARIABLE_MONTHS:]
    if recent:
        fc.variable_median = _r(median(variable_by_month.get(m, 0.0) for m in recent))
        fc.variable_mean = _r(sum(variable_by_month.get(m, 0.0) for m in recent) / len(recent))
        fc.income_variable = _r(median(var_income_by_month.get(m, 0.0) for m in recent))
    fc.monthly_split = [MonthSplit(y, m, _r(fixed_by_month.get((y, m), 0.0)),
                                   _r(variable_by_month.get((y, m), 0.0)),
                                   _r(fixed_income_by_month.get((y, m), 0.0)
                                      + var_income_by_month.get((y, m), 0.0))) for y, m in window]

    _build_series(fc, cats, charges, window_set, current, today)
    _build_transfers(fc, transfers, window, current)
    return fc


def _build_series(fc: Forecast, cats: Dict[int, _Cat], charges, window_set: Set[Month],
                  current: Month, today: date) -> None:
    following = _add_months(current, 1)
    months = max(1, fc.months)
    for (kind, category_id, _key), rows in charges.items():
        rows.sort(key=lambda r: r[0])
        dates = [d for d, _v, _t in rows]
        # Several charges on the same day are one bill (a split receipt).
        by_day: Dict[date, float] = defaultdict(float)
        for d, v, _t in rows:
            by_day[d] += v
        period = _period_of(dates)
        if period is None and len(by_day) == 1 and fc.months >= 11:
            period = 12                                # one charge in a full year: annual
        amounts = [by_day[d] for d in sorted(by_day)]
        usual = amounts[-1] if period == 12 else median(amounts[-3:])
        last_date = max(by_day)
        next_due = _add_months_to_date(last_date, period) if period else None
        active = True
        if period:
            active = (today - last_date).days <= period * 30.44 * 1.5
        in_window = sum(v for d, v, _t in rows if _month_of(d) in window_set)
        category = cats[category_id].name if category_id in cats else UNCATEGORIZED_LABEL
        series = Series(
            label=rows[-1][2][:40].strip() or category, category=category, kind=kind, period=period,
            usual=_r(usual), monthly=_r(in_window / months), last_date=last_date,
            next_due=next_due, active=active, charges=len(by_day),
        )
        fc.series.append(series)

        this_month = sum(v for d, v, _t in rows if _month_of(d) == current)
        charged = any(_month_of(d) == current for d in by_day)
        if active and period:
            if charged:
                above = None
                if (usual > 0 and this_month > usual * (1 + ABOVE_USUAL)
                        and this_month - usual >= ABOVE_USUAL_MIN_EUR):
                    above = round((this_month / usual - 1) * 100)
                fc.this_month.append(ForecastItem(series.label, category, kind, series.usual, period,
                                                  STATUS_CHARGED, _r(this_month), last_date.day, above))
            elif next_due is not None and _month_of(next_due) <= current:
                fc.this_month.append(ForecastItem(series.label, category, kind, series.usual, period,
                                                  STATUS_PENDING, None, next_due.day))
            # Next month: the next charge after the latest one (or after the
            # pending one of this month).
            due = next_due
            while due is not None and _month_of(due) <= current:
                due = _add_months_to_date(due, period)
            if due is not None and _month_of(due) == following:
                fc.next_month.append(ForecastItem(series.label, category, kind, series.usual, period,
                                                  day=due.day))
        elif not period and kind == "expense":
            fc.next_other_fixed += series.monthly          # irregular: its average
        elif not period:
            fc.next_other_income += series.monthly         # irregular fixed income: its average

    fc.next_other_fixed = _r(fc.next_other_fixed)
    fc.next_fixed = _r(sum(i.expected for i in fc.next_month if i.kind == "expense"))
    fc.next_other_income = _r(fc.next_other_income)
    fc.next_income = _r(fc.next_other_income + fc.income_variable
                        + sum(i.expected for i in fc.next_month if i.kind == "income"))
    fc.series.sort(key=lambda s: (s.kind != "expense", not s.active, -s.monthly, s.label))
    fc.this_month.sort(key=lambda i: (i.kind != "expense", i.status != STATUS_PENDING, i.day or 0))
    fc.next_month.sort(key=lambda i: (i.kind != "expense", i.day or 0))


def _build_transfers(fc: Forecast, transfers: Dict[int, TransferLine], window: List[Month],
                     current: Month) -> None:
    fc.transfers = sorted(transfers.values(), key=lambda line: (line.direction, line.category))
    months = max(1, fc.months)
    for line in fc.transfers:
        total = sum(line.by_month.get(m, 0.0) for m in window)
        if line.direction == "in":
            fc.transfers_in_avg += total / months
        else:
            fc.transfers_out_avg += total / months
    fc.transfers_in_avg = _r(fc.transfers_in_avg)
    fc.transfers_out_avg = _r(fc.transfers_out_avg)
    fc.contributions = contributions(fc.transfers, window, current)
    fc.next_transfers = expected_transfers(fc.transfers, window)


def expected_transfers(lines: Sequence[TransferLine], window: List[Month]) -> List[ForecastItem]:
    """Next month's aportes, one per member and direction: the median of
    what that member sent / received over the last VARIABLE_MONTHS complete
    months (a missed or doubled month does not move it)."""
    recent = window[-VARIABLE_MONTHS:]
    grouped: Dict[Tuple[str, str], Dict[Month, float]] = defaultdict(lambda: defaultdict(float))
    categories: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    linked: Set[Tuple[str, str]] = set()
    for line in lines:
        key = (line.direction, line.peer or line.category)
        if line.peer:
            linked.add(key)
        categories[key].append(line.category)
        for month, amount in line.by_month.items():
            grouped[key][month] += amount
    items: List[ForecastItem] = []
    for (direction, who), by_month in sorted(grouped.items()):
        expected = _r(median(by_month.get(m, 0.0) for m in recent)) if recent else 0.0
        if expected <= 0:
            continue
        if (direction, who) not in linked:
            label = who                                   # no «Con» yet: the category's name
        else:
            label = f"Aportación de {who}" if direction == "in" else f"Aportación a {who}"
        items.append(ForecastItem(label, ", ".join(sorted(set(categories[(direction, who)]))),
                                  "transfer_in" if direction == "in" else "transfer_out", expected))
    return items


# ---------------------------------------------------------------------------
# Aportaciones: who sends what to whom
# ---------------------------------------------------------------------------

def month_label(month: Month) -> str:
    return f"{MONTH_NAMES[month[1]][:3]} {str(month[0])[2:]}"


def contributions(lines: Sequence[TransferLine], window: List[Month], current: Month) -> List[Contribution]:
    """One row per (sender → receiver) from the traspaso lines available.

    The receiver's own record («Aporte Comun María» in COMUN, linked to
    MARIA) is preferred; the sender's («Aporte común» in MARIA, linked to
    COMUN) is used when the receiver has none. With both, months where they
    disagree are listed (a transfer made on the 31st that arrives on the 1st
    cancels out over the two months and is not reported).
    """
    received: Dict[Tuple[str, str], Dict[Month, float]] = defaultdict(lambda: defaultdict(float))
    sent: Dict[Tuple[str, str], Dict[Month, float]] = defaultdict(lambda: defaultdict(float))
    for line in lines:
        if line.direction == "in":
            pair = (line.peer or f"sin indicar ({line.category})", line.owner)
            target = received
        else:
            pair = (line.owner, line.peer or f"sin indicar ({line.category})")
            target = sent
        for month, amount in line.by_month.items():
            target[pair][month] += amount

    last = _add_months(current, -1)
    months = max(1, len(window))
    rows: List[Contribution] = []
    for pair in sorted(set(received) | set(sent)):
        source = received.get(pair) or sent.get(pair)
        total = sum(source.get(m, 0.0) for m in window)
        row = Contribution(
            sender=pair[0], receiver=pair[1], this_month=_r(source.get(current, 0.0)),
            last_month=_r(source.get(last, 0.0)), average=_r(total / months), total=_r(total),
            share_pct=None, by_month={m: _r(source.get(m, 0.0)) for m in window + [current]},
        )
        if pair in received and pair in sent:
            diffs = [sent[pair].get(m, 0.0) - received[pair].get(m, 0.0) for m in window]
            for i, (month, diff) in enumerate(zip(window, diffs)):
                if abs(diff) < 1:
                    continue
                before = diffs[i - 1] if i > 0 else 0.0
                after = diffs[i + 1] if i + 1 < len(diffs) else 0.0
                if abs(diff + before) < 1 or abs(diff + after) < 1:
                    continue                                   # crossed a month end
                rows_note = (f"{month_label(month)}: enviado {sent[pair].get(month, 0.0):,.2f} €, "
                             f"recibido {received[pair].get(month, 0.0):,.2f} €")
                row.mismatches.append(rows_note.replace(",", "X").replace(".", ",").replace("X", "."))
        rows.append(row)

    by_receiver: Dict[str, float] = defaultdict(float)
    for row in rows:
        by_receiver[row.receiver] += row.total
    for row in rows:
        if by_receiver[row.receiver] > 0:
            row.share_pct = round(row.total / by_receiver[row.receiver] * 100, 1)
    return rows


# ---------------------------------------------------------------------------
# Familia
# ---------------------------------------------------------------------------

def family_member_ids(session: Session) -> List[int]:
    """Every member except the demo account (shown to visitors)."""
    return [uid for (uid,) in session.query(User.id).order_by(User.id) if shares_global_rules(session, uid)]


def family_forecast(session: Session, today: Optional[date] = None) -> Forecast:
    """The household: every member's forecast added up. Only aggregates —
    sums per category and per bill name — never anyone's movements.
    Traspasos are already out of each member's totals, so money moved
    between members is not counted twice; aportes appear in
    `contributions` (one row per sender → receiver)."""
    today = today or date.today()
    members = family_member_ids(session)
    names = dict(session.query(User.id, User.username))
    parts = [user_forecast(session, uid, today) for uid in members]
    window_months = sorted({m for p in parts for m in p.window})
    fam = Forecast(today=today, months=len(window_months), window=window_months,
                   members=[names[uid] for uid in members])
    if not parts:
        return fam

    # Each part is already per month: the household is their sum.
    fam.fixed_baseline = _r(sum(p.fixed_baseline for p in parts))
    by_cat: Dict[str, float] = defaultdict(float)
    for p in parts:
        for name, value in p.fixed_by_category:
            by_cat[name] += value
    fam.fixed_by_category = sorted(((n, _r(v)) for n, v in by_cat.items()), key=lambda item: -item[1])
    fam.series = sorted((s for p in parts for s in p.series),
                        key=lambda s: (s.kind != "expense", not s.active, -s.monthly, s.label))
    fam.variable_median = _r(sum(p.variable_median for p in parts))
    fam.variable_mean = _r(sum(p.variable_mean for p in parts))
    fam.income_fixed = _r(sum(p.income_fixed for p in parts))
    fam.income_variable = _r(sum(p.income_variable for p in parts))
    split: Dict[Month, List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for p in parts:
        for s in p.monthly_split:
            split[(s.year, s.month)][0] += s.fixed
            split[(s.year, s.month)][1] += s.variable
            split[(s.year, s.month)][2] += s.income
    fam.monthly_split = [MonthSplit(y, m, *(_r(v) for v in split[(y, m)])) for y, m in window_months]
    fam.this_month = sorted((i for p in parts for i in p.this_month),
                            key=lambda i: (i.kind != "expense", i.status != STATUS_PENDING, i.day or 0))
    fam.next_month = sorted((i for p in parts for i in p.next_month),
                            key=lambda i: (i.kind != "expense", i.day or 0))
    fam.next_fixed = _r(sum(p.next_fixed for p in parts))
    fam.next_other_fixed = _r(sum(p.next_other_fixed for p in parts))
    fam.next_other_income = _r(sum(p.next_other_income for p in parts))
    fam.next_income = _r(sum(p.next_income for p in parts))
    # next_transfers stays empty: between members they cancel out.
    fam.uncategorized = sum(p.uncategorized for p in parts)
    lines = [line for p in parts for line in p.transfers]
    fam.contributions = contributions(lines, window_months, _month_of(today))
    fam.warnings = _family_warnings(lines, names, members)
    return fam


def _family_warnings(lines: Sequence[TransferLine], names: Dict[int, str], members: List[int]) -> List[str]:
    """Aportes recorded as traspaso on one side only are counted twice in the
    family totals (a bill on one side, spending on the other)."""
    warnings: List[str] = []
    sends = {(line.owner_id, line.peer_id) for line in lines if line.direction == "out"}
    gets = {(line.owner_id, line.peer_id) for line in lines if line.direction == "in"}
    for owner, peer in sorted(gets, key=lambda p: (p[0], p[1] or 0)):
        if peer in members and (peer, owner) not in sends:
            warnings.append(
                f"{names[owner]} recibe aportes de {names[peer]}, pero en {names[peer]} ninguna categoría "
                f"de traspaso apunta a {names[owner]}: si su aporte cuenta como gasto, se suma dos veces.")
    for owner, peer in sorted(sends, key=lambda p: (p[0], p[1] or 0)):
        if peer in members and (peer, owner) not in gets:
            warnings.append(
                f"{names[owner]} envía aportes a {names[peer]}, pero en {names[peer]} ninguna categoría "
                f"de traspaso apunta a {names[owner]}: si se cuenta como ingreso, se suma dos veces.")
    if any(line.peer_id is None for line in lines):
        warnings.append("Hay categorías de traspaso sin «Con»: indica con quién para ver las aportaciones "
                        "por miembro.")
    return warnings
