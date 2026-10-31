"""
tests/test_forecast.py
======================
Previsiones: fixed baseline, bill frequencies, this/next month, variable
estimate, traspasos and aportes, and the Familia aggregate — on a household
shaped like the real one (two parents + a joint account + the demo user).
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analytics.forecast import (
    STATUS_CHARGED,
    STATUS_PENDING,
    _period_of,
    family_forecast,
    family_member_ids,
    series_key,
    user_forecast,
)
from app.db.models import (
    CATEGORY_NATURE_FIXED,
    CATEGORY_NATURE_TRANSFER,
    FIXED_OVERRIDE_FIXED,
    Account,
    Base,
    Category,
    CategoryLevel,
    Transaction,
    TransactionType,
    User,
)

TODAY = date(2026, 10, 20)
WINDOW = [(2025, m) for m in (10, 11, 12)] + [(2026, m) for m in range(1, 10)]


def months(start=(2025, 10), end=(2026, 10)):
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class Home:
    def __init__(self, db: Session):
        self.db = db
        self.users = {}
        self.accounts = {}
        self.types = {}

    def user(self, name):
        user = User(username=name, password_hash="x")
        self.db.add(user)
        self.db.flush()
        account = Account(user_id=user.id, account_name=f"Cuenta {name}")
        self.db.add(account)
        self.db.flush()
        self.users[name], self.accounts[name] = user, account
        for type_name, kind in (("GASTO", TransactionType.expense), ("INGRESO", TransactionType.income),
                                ("INVERSION", TransactionType.investment)):
            node = Category(user_id=user.id, name=type_name, level=CategoryLevel.type, transaction_type=kind)
            self.db.add(node)
            self.db.flush()
            self.types[(name, type_name)] = node
        return user

    def cat(self, owner, type_name, name, nature=None, peer=None):
        cat = Category(user_id=self.users[owner].id, parent_id=self.types[(owner, type_name)].id, name=name,
                       level=CategoryLevel.category, nature=nature,
                       transfer_peer_user_id=self.users[peer].id if peer else None)
        self.db.add(cat)
        self.db.flush()
        return cat

    def txn(self, owner, cat, when, amount, issuer, **kw):
        t = Transaction(user_id=self.users[owner].id, account_id=self.accounts[owner].id,
                        transaction_date=when, amount=amount, issuer_name=issuer,
                        type=TransactionType.expense if amount < 0 else TransactionType.income,
                        category_id=cat.id if cat is not None else None, **kw)
        self.db.add(t)
        return t


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def home(db):
    h = Home(db)
    for name in ("COMUN", "MARIA", "PEDRO", "DEMO"):
        h.user(name)

    # --- COMUN: the joint account pays the house --------------------------
    hipoteca = h.cat("COMUN", "GASTO", "Hipoteca", CATEGORY_NATURE_FIXED)
    luz = h.cat("COMUN", "GASTO", "Luz", CATEGORY_NATURE_FIXED)
    seguro = h.cat("COMUN", "GASTO", "Seguro hogar", CATEGORY_NATURE_FIXED)
    gas = h.cat("COMUN", "GASTO", "Gas", CATEGORY_NATURE_FIXED)
    internet = h.cat("COMUN", "GASTO", "Internet", CATEGORY_NATURE_FIXED)
    netflix = h.cat("COMUN", "GASTO", "Netflix", CATEGORY_NATURE_FIXED)
    super_ = h.cat("COMUN", "GASTO", "Supermercados")
    fondos = h.cat("COMUN", "INVERSION", "Fondos")
    aporte_m = h.cat("COMUN", "INGRESO", "Aporte Comun María", CATEGORY_NATURE_TRANSFER, "MARIA")
    aporte_p = h.cat("COMUN", "INGRESO", "Aporte Comun Pedro", CATEGORY_NATURE_TRANSFER, "PEDRO")

    h.txn("COMUN", seguro, date(2025, 3, 15), -600, "MAPFRE SEGUROS")       # first movement
    h.txn("COMUN", seguro, date(2026, 3, 15), -600, "MAPFRE SEGUROS")
    for y, m in months():
        h.txn("COMUN", hipoteca, date(y, m, 5), -900, "PRESTAMO 0012345")
        h.txn("COMUN", aporte_m, date(y, m, 1), 600 if (y, m) == (2026, 6) else 700, "TRANSF MARIA")
        h.txn("COMUN", aporte_p, date(y, m, 1), 500, "TRANSF PEDRO")
        h.txn("COMUN", internet, date(y, m, 2), -60 if (y, m) == (2026, 10) else -40, f"DIGI {m:02d}/{y}")
        if (y, m) != (2026, 10):
            h.txn("COMUN", gas, date(y, m, 25), -50, "NATURGY")              # October still pending
            h.txn("COMUN", super_, date(y, m, 12), -1400 if (y, m) == (2026, 7) else -400, "MERCADONA")
        if (y, m) <= (2026, 6):
            h.txn("COMUN", netflix, date(y, m, 8), -18, "NETFLIX.COM")       # cancelled in July
    for y, m in [(2025, 11), (2026, 1), (2026, 3), (2026, 5), (2026, 7), (2026, 9)]:
        h.txn("COMUN", luz, date(y, m, 10), -80, "IBERDROLA CLIENTES")       # every two months
    h.txn("COMUN", luz, date(2026, 9, 12), 20, "IBERDROLA CLIENTES")          # a refund
    h.txn("COMUN", fondos, date(2026, 2, 3), -1000, "MYINVESTOR")
    h.txn("COMUN", None, date(2026, 8, 3), -15, "CAFETERIA")                 # uncategorized

    # --- MARIA: salary, and her aporte recorded as a traspaso to COMUN -----
    nomina = h.cat("MARIA", "INGRESO", "Nómina", CATEGORY_NATURE_FIXED)
    aporte = h.cat("MARIA", "GASTO", "Aporte común", CATEGORY_NATURE_TRANSFER, "COMUN")
    ropa = h.cat("MARIA", "GASTO", "Ropa")
    for y, m in months():
        h.txn("MARIA", nomina, date(y, m, 28) if (y, m) != (2026, 10) else date(2026, 10, 19), 2000, "NOMINA ACME")
        if (y, m) == (2026, 8):
            h.txn("MARIA", aporte, date(2026, 8, 1), -700, "TRANSF A COMUN")
            h.txn("MARIA", aporte, date(2026, 8, 31), -700, "TRANSF A COMUN")  # September's, early
        elif (y, m) != (2026, 9):
            h.txn("MARIA", aporte, date(y, m, 1), -700, "TRANSF A COMUN")
        h.txn("MARIA", ropa, date(y, m, 14), -100, "ZARA")

    # --- PEDRO: his aporte is still an ordinary expense ---------------------
    aporte_ped = h.cat("PEDRO", "GASTO", "Aporte común")
    for y, m in months():
        h.txn("PEDRO", aporte_ped, date(y, m, 1), -500, "TRANSF A COMUN")

    # --- DEMO: never part of the family -------------------------------------
    demo_cat = h.cat("DEMO", "GASTO", "Alquiler", CATEGORY_NATURE_FIXED)
    for y, m in months():
        h.txn("DEMO", demo_cat, date(y, m, 1), -750, "INMOBILIARIA")
    db.commit()
    return h


def item(items, label):
    return next(i for i in items if i.label.startswith(label))


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def test_series_key_drops_references_and_dates():
    assert series_key("IBERDROLA CLIENTES, S.A.U. 0012345") == series_key("Iberdrola Clientes SAU 99")
    assert series_key("DIGI 09/2026") == series_key("DIGI 10/2026") == "digi"


@pytest.mark.parametrize("dates, expected", [
    ([date(2026, 1, 31), date(2026, 3, 1), date(2026, 3, 31)], 1),     # month ends jitter
    ([date(2026, 1, 10), date(2026, 3, 12), date(2026, 5, 9)], 2),
    ([date(2026, 1, 10), date(2026, 4, 10), date(2026, 7, 10)], 3),
    ([date(2025, 3, 15), date(2026, 3, 15)], 12),
    ([date(2026, 1, 10), date(2026, 1, 30), date(2026, 4, 2)], None),   # no pattern
    ([date(2026, 1, 10)], None),
])
def test_period_detection(dates, expected):
    assert _period_of(dates) == expected


# ---------------------------------------------------------------------------
# One account
# ---------------------------------------------------------------------------

def test_joint_account_baseline_spreads_and_nets(db, home):
    fc = user_forecast(db, home.users["COMUN"].id, TODAY)
    assert fc.window == WINDOW and fc.months == 12 and not fc.provisional
    # 12 × 900 hipoteca + luz 6 × 80 − 20 refund + seguro 600 (annual, spread)
    # + gas 12 × 50 + internet 12 × 40 + netflix 9 × 18; October never counts;
    # the investment and the aportes are out.
    assert fc.fixed_baseline == pytest.approx((10800 + 460 + 600 + 600 + 480 + 162) / 12, abs=0.01)
    assert dict(fc.fixed_by_category)["Seguro hogar"] == 50.0
    assert dict(fc.fixed_by_category)["Luz"] == pytest.approx(460 / 12, abs=0.01)
    # Variable: median of the last 6 months (400 400 400 1400 415 400)
    # ignores July's 1400; the uncategorized café counts as variable.
    assert fc.variable_median == 400
    assert fc.variable_mean == pytest.approx((400 * 5 + 1400 + 15) / 6, abs=0.01)
    assert fc.variable_mean > fc.variable_median
    assert fc.uncategorized == 1


def test_series_frequency_and_activity(db, home):
    fc = user_forecast(db, home.users["COMUN"].id, TODAY)
    by_label = {s.label: s for s in fc.series}
    assert by_label["IBERDROLA CLIENTES"].period == 2
    assert by_label["IBERDROLA CLIENTES"].usual == 80          # the refund is not a charge
    assert by_label["MAPFRE SEGUROS"].period == 12
    assert by_label["PRESTAMO 0012345"].period == 1
    assert not by_label["NETFLIX.COM"].active                  # no charge since June


def test_this_month_checklist(db, home):
    fc = user_forecast(db, home.users["COMUN"].id, TODAY)
    hipoteca = item(fc.this_month, "PRESTAMO")
    assert (hipoteca.status, hipoteca.actual) == (STATUS_CHARGED, 900)
    gas = item(fc.this_month, "NATURGY")
    assert (gas.status, gas.expected, gas.day) == (STATUS_PENDING, 50, 25)
    internet = item(fc.this_month, "DIGI")
    assert internet.status == STATUS_CHARGED and internet.above_pct == 50
    assert not any(i.label.startswith(("IBERDROLA", "NETFLIX", "MAPFRE")) for i in fc.this_month)
    assert fc.pending_this_month == 50


def test_next_month_only_shows_bills_due_then(db, home):
    fc = user_forecast(db, home.users["COMUN"].id, TODAY)
    labels = {i.label.split()[0] for i in fc.next_month}
    assert labels == {"PRESTAMO", "IBERDROLA", "NATURGY", "DIGI"}   # no annual insurance, no Netflix
    assert fc.next_fixed == 900 + 80 + 50 + 40
    assert fc.next_month_label == "noviembre 2026"
    assert fc.next_expenses == pytest.approx(fc.next_fixed + fc.variable_median)


def test_joint_account_aportes_and_required_contribution(db, home):
    fc = user_forecast(db, home.users["COMUN"].id, TODAY)
    assert fc.receives_transfers
    assert fc.transfers_in_avg == pytest.approx((700 * 12 - 100 + 500 * 12) / 12, abs=0.01)
    assert fc.required_contribution == pytest.approx(fc.fixed_baseline + fc.variable_median, abs=0.01)
    assert fc.contribution_gap == pytest.approx(fc.transfers_in_avg - fc.required_contribution, abs=0.01)
    rows = {(c.sender, c.receiver): c for c in fc.contributions}
    assert set(rows) == {("MARIA", "COMUN"), ("PEDRO", "COMUN")}
    assert rows[("MARIA", "COMUN")].total == 8300
    assert rows[("MARIA", "COMUN")].share_pct == pytest.approx(8300 / 14300 * 100, abs=0.1)
    assert rows[("MARIA", "COMUN")].this_month == 700
    assert not rows[("MARIA", "COMUN")].mismatches              # only one side here


def test_parent_sees_only_their_own_aportes(db, home):
    fc = user_forecast(db, home.users["MARIA"].id, TODAY)
    assert [(c.sender, c.receiver) for c in fc.contributions] == [("MARIA", "COMUN")]
    assert fc.transfers_out_avg == pytest.approx(700, abs=0.01)
    assert fc.fixed_baseline == 0                                # the aporte is not a bill
    assert fc.income_fixed == 2000
    assert item(fc.this_month, "NOMINA").status == STATUS_CHARGED
    assert not fc.receives_transfers


def test_movement_override_makes_a_variable_category_fixed(db, home):
    maria = home.users["MARIA"]
    zara = db.query(Transaction).filter_by(user_id=maria.id, issuer_name="ZARA").all()
    for t in zara:
        t.fixed_override = FIXED_OVERRIDE_FIXED
    db.commit()
    fc = user_forecast(db, maria.id, TODAY)
    assert fc.fixed_baseline == 100
    assert fc.variable_median == 0


def test_little_history_is_provisional_or_not_enough(db):
    h = Home(db)
    h.user("NUEVO")
    luz = h.cat("NUEVO", "GASTO", "Luz", CATEGORY_NATURE_FIXED)
    h.txn("NUEVO", luz, date(2026, 8, 10), -80, "IBERDROLA")
    db.commit()
    fc = user_forecast(db, h.users["NUEVO"].id, TODAY)
    assert fc.months == 2 and not fc.enough_data
    h.txn("NUEVO", luz, date(2026, 6, 10), -80, "IBERDROLA")
    db.commit()
    fc = user_forecast(db, h.users["NUEVO"].id, TODAY)
    assert fc.months == 4 and fc.enough_data and fc.provisional
    assert fc.fixed_baseline == 40                              # 160 € over 4 months


def test_empty_user_has_no_data(db):
    h = Home(db)
    h.user("VACIO")
    db.commit()
    fc = user_forecast(db, h.users["VACIO"].id, TODAY)
    assert fc.months == 0 and not fc.enough_data and fc.series == []


# ---------------------------------------------------------------------------
# Familia
# ---------------------------------------------------------------------------

def test_family_never_includes_demo(db, home):
    assert family_member_ids(db) == [home.users[n].id for n in ("COMUN", "MARIA", "PEDRO")]
    fam = family_forecast(db, TODAY)
    assert fam.members == ["COMUN", "MARIA", "PEDRO"]
    assert not any(s.label == "INMOBILIARIA" for s in fam.series)


def test_family_adds_up_without_counting_traspasos(db, home):
    comun = user_forecast(db, home.users["COMUN"].id, TODAY)
    maria = user_forecast(db, home.users["MARIA"].id, TODAY)
    pedro = user_forecast(db, home.users["PEDRO"].id, TODAY)
    fam = family_forecast(db, TODAY)
    assert fam.fixed_baseline == pytest.approx(comun.fixed_baseline)
    assert fam.income_fixed == 2000
    # MARIA's aporte is a traspaso on both sides: in nobody's spending.
    assert maria.variable_median == 100
    # PEDRO's is still an expense on his side: it is counted (and warned about).
    assert pedro.variable_median == 500
    assert fam.variable_median == pytest.approx(comun.variable_median + 100 + 500)
    assert any("PEDRO" in w and "dos veces" in w for w in fam.warnings)
    assert not any("MARIA" in w for w in fam.warnings)


def test_family_aportes_reconcile_both_sides(db, home):
    fam = family_forecast(db, TODAY)
    rows = {(c.sender, c.receiver): c for c in fam.contributions}
    maria = rows[("MARIA", "COMUN")]
    assert maria.total == 8300                                  # COMUN's record wins
    # June: 700 sent, 600 received → reported. August/September: sent on
    # 31 Aug, received on 1 Sep → cancels out, not reported.
    assert len(maria.mismatches) == 1 and maria.mismatches[0].startswith("jun 26")
    assert "700,00" in maria.mismatches[0] and "600,00" in maria.mismatches[0]
    assert rows[("PEDRO", "COMUN")].share_pct + maria.share_pct == pytest.approx(100, abs=0.2)


# ---------------------------------------------------------------------------
# Ingresos and gastos never mixed
# ---------------------------------------------------------------------------

def test_income_and_expense_lists_never_mix(db, home):
    for name in ("COMUN", "MARIA"):
        fc = user_forecast(db, home.users[name].id, TODAY)
        assert all(i.kind == "income" for i in fc.this_month_income + fc.next_month_income)
        assert all(i.kind == "expense" for i in fc.this_month_expenses + fc.next_month_expenses)
        assert all(s.kind == "income" for s in fc.income_series)
        assert all(s.kind == "expense" for s in fc.expense_series)
    maria = user_forecast(db, home.users["MARIA"].id, TODAY)
    assert [i.label for i in maria.next_month_income] == ["NOMINA ACME"]
    assert maria.next_month_expenses == []
    assert maria.next_fixed_income == 2000
    assert maria.next_income == 2000 and maria.next_expenses == 100     # Zara, variable
    assert maria.this_month_income[0].status == STATUS_CHARGED
    assert all(s.income == 2000 for s in maria.monthly_split)


def test_joint_account_expects_each_members_aporte(db, home):
    fc = user_forecast(db, home.users["COMUN"].id, TODAY)
    expected = {i.label: i.expected for i in fc.next_transfers_in}
    # Median of the last 6 months: June's 600 does not move MARIA's 700.
    assert expected == {"Aportación de MARIA": 700, "Aportación de PEDRO": 500}
    assert fc.next_transfers_out == []
    assert fc.next_income == 0                                   # no income of its own
    assert fc.next_income_with_transfers == 1200
    assert fc.next_saving_with_transfers == pytest.approx(1200 - fc.next_expenses, abs=0.01)


def test_parent_expects_their_aporte_as_money_out(db, home):
    fc = user_forecast(db, home.users["MARIA"].id, TODAY)
    assert [(i.label, i.expected) for i in fc.next_transfers_out] == [("Aportación a COMUN", 700)]
    assert fc.next_expenses_with_transfers == 800
    assert fc.next_saving == 1900 and fc.next_saving_with_transfers == 1200


def test_family_has_no_expected_aportes(db, home):
    fam = family_forecast(db, TODAY)
    assert fam.next_transfers == []
    assert fam.next_saving == fam.next_saving_with_transfers
    assert fam.next_income == pytest.approx(2000)               # MARIA's salary only
    assert all(s.income == 2000 for s in fam.monthly_split)


def test_unlinked_traspaso_keeps_its_category_name(db, home):
    maria = home.users["MARIA"]
    aporte = db.query(Category).filter_by(user_id=maria.id, name="Aporte común").one()
    aporte.transfer_peer_user_id = None
    db.commit()
    fc = user_forecast(db, maria.id, TODAY)
    assert [i.label for i in fc.next_transfers_out] == ["Aporte común"]
