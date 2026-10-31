"""
tests/test_periods_and_session.py
=================================
The web period selector (Mes / Trimestre / Año / 12 meses / Todo /
Personalizado with previous / next) and the in-process session holder.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.auth.session_manager import SessionManager
from app.db.models import User
from app.web.periods import resolve_period

TODAY = date(2026, 10, 31)


def test_month_is_the_default_and_navigates():
    p = resolve_period(None, today=TODAY)
    assert (p.key, p.date_from, p.date_to, p.label) == ("mes", date(2026, 10, 1), date(2026, 10, 31), "Octubre 2026")
    assert p.prev_ref == date(2026, 9, 1) and p.next_ref is None          # no future months
    feb = resolve_period("mes", ref=date(2024, 2, 10), today=TODAY)
    assert feb.date_to == date(2024, 2, 29) and feb.next_ref == date(2024, 3, 1)
    dec = resolve_period("mes", ref=date(2025, 12, 5), today=TODAY)
    assert dec.date_to == date(2025, 12, 31) and dec.next_ref == date(2026, 1, 1)


def test_quarter_and_year():
    q = resolve_period("trimestre", ref=date(2026, 5, 20), today=TODAY)
    assert (q.date_from, q.date_to, q.label) == (date(2026, 4, 1), date(2026, 6, 30), "T2 2026")
    assert q.prev_ref == date(2026, 1, 1) and q.next_ref == date(2026, 7, 1)
    current = resolve_period("trimestre", ref=TODAY, today=TODAY)
    assert current.label == "T4 2026" and current.next_ref is None
    y = resolve_period("anio", ref=date(2025, 3, 1), today=TODAY)
    assert (y.date_from, y.date_to, y.label) == (date(2025, 1, 1), date(2025, 12, 31), "2025")
    assert y.next_ref == date(2026, 1, 1)
    assert resolve_period("anio", ref=TODAY, today=TODAY).next_ref is None


def test_rolling_all_and_custom():
    r = resolve_period("12m", today=TODAY)
    assert (r.date_from, r.date_to) == (date(2025, 11, 1), TODAY)
    assert resolve_period("todo", today=TODAY).date_from == date(2000, 1, 1)
    c = resolve_period("custom", custom_from=date(2026, 6, 30), custom_to=date(2026, 3, 1), today=TODAY)
    assert (c.key, c.date_from, c.date_to) == ("custom", date(2026, 3, 1), date(2026, 6, 30))
    assert c.label == "01/03/2026 – 30/06/2026"
    # Custom without both dates falls back to the month.
    assert resolve_period("custom", custom_from=date(2026, 3, 1), today=TODAY).key == "mes"


def test_session_manager_holds_one_user():
    SessionManager.logout()
    assert not SessionManager.is_authenticated() and SessionManager.get_current_user() is None
    with pytest.raises(RuntimeError):
        SessionManager.require_user()
    ana = User(username="ana", password_hash="x")
    SessionManager.login(ana)
    assert SessionManager.is_authenticated() and SessionManager.require_user() is ana
    SessionManager.logout()
    assert SessionManager.get_current_user() is None
