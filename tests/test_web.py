"""
tests/test_web.py
=================
LAN web front-end: authentication, per-user isolation, CSRF/origin checks,
security headers, pages, light edits and the shared services behind them.
"""
from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.analytics.dashboard_summary import dashboard_summary
from app.auth.auth_service import create_user
from app.categorization.category_manager import assign_user_transaction_category
from app.db.models import (
    CATEGORY_SOURCE_MANUAL,
    Account,
    AssetType,
    AuditLog,
    AutoCategorizationRule,
    Base,
    Category,
    CategoryLevel,
    InvestmentKind,
    InvestmentTransaction,
    MatchType,
    Security,
    Transaction,
    TransactionType,
)
from app.web.app import create_app
from app.web.deps import get_db
from app.web.security import SESSION_COOKIE, LoginLimiter, SessionStore

BASE = "https://testserver"
ORIGIN = {"Origin": BASE}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys = ON"))
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


@pytest.fixture
def family(factory):
    db = factory()
    ana = create_user(db, "ana", "clave-ana")
    beto = create_user(db, "beto", "clave-beto")
    ids = {"ana": ana.id, "beto": beto.id}
    for user in (ana, beto):
        account = Account(user_id=user.id, account_name=f"Cuenta {user.username}")
        db.add(account)
        db.flush()
        gasto = Category(user_id=user.id, name="GASTO", level=CategoryLevel.type,
                         transaction_type=TransactionType.expense)
        db.add(gasto)
        db.flush()
        super_ = Category(user_id=user.id, parent_id=gasto.id, name="Supermercados",
                          level=CategoryLevel.category)
        db.add(super_)
        db.flush()
        ids[f"{user.username}_cat"] = super_.id
        txn = Transaction(user_id=user.id, account_id=account.id, transaction_date=date.today(),
                          amount=-42.5, running_balance=1000.0, type=TransactionType.expense,
                          issuer_name=f"MERCADO {user.username.upper()}")
        nomina = Transaction(user_id=user.id, account_id=account.id, transaction_date=date.today(),
                             amount=2000.0, type=TransactionType.income, issuer_name="NOMINA")
        db.add_all([txn, nomina])
        db.flush()
        ids[f"{user.username}_txn"] = txn.id
    security = Security(symbol="ACME", name="Acme Corp", asset_type=AssetType.stock, currency="EUR")
    db.add(security)
    db.flush()
    buy = InvestmentTransaction(user_id=beto.id, security_id=security.id, broker="DEGIRO",
                                kind=InvestmentKind.buy, trade_date=date(2025, 1, 10),
                                quantity=10, price=10, currency="EUR", amount_eur=100)
    db.add(buy)
    db.commit()
    ids["security"] = security.id
    ids["beto_op"] = buy.id
    db.close()
    return ids


@pytest.fixture
def app(factory, family):
    changes = []
    application = create_app(sessions=SessionStore(), limiter=LoginLimiter(),
                             on_change=lambda uid, area: changes.append((uid, area)), lan_only=True)

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = _db
    application.state.changes = changes
    return application


def client_for(app) -> TestClient:
    # TestClient's peer address is "testclient"; present a LAN address.
    return TestClient(app, base_url=BASE, client=("192.168.1.50", 50000))


def login(client: TestClient, username: str, password: str):
    return client.post("/login", data={"username": username, "password": password},
                       headers=ORIGIN, follow_redirects=False)


def logged_in(app, username="ana", password="clave-ana") -> TestClient:
    client = client_for(app)
    response = login(client, username, password)
    assert response.status_code == 303, response.text
    return client


def csrf_of(client: TestClient) -> str:
    page = client.get("/").text
    return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_pages_require_login(app):
    client = client_for(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.get("/api/resumen").status_code == 401


def test_login_sets_hardened_session_cookie(app):
    client = client_for(app)
    response = login(client, "ana", "clave-ana")
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()
    assert "domain" not in cookie.lower()
    assert "Hola, ana" in client.get("/").text


def test_wrong_password_is_generic_and_rate_limited(app):
    client = client_for(app)
    bad = login(client, "ana", "nope")
    assert bad.status_code == 401
    assert "Usuario o contraseña incorrectos" in bad.text
    unknown = login(client, "nadie", "nope")
    assert "Usuario o contraseña incorrectos" in unknown.text
    for _ in range(3):
        login(client, "ana", "nope")
    blocked = login(client, "ana", "clave-ana")          # even the right password waits
    assert blocked.status_code == 429


def test_login_rotates_session_and_logout_revokes_it(app):
    client = client_for(app)
    client.cookies.set(SESSION_COOKIE, "attacker-chosen-id", domain="testserver.local")
    login(client, "ana", "clave-ana")
    sid = client.cookies.get(SESSION_COOKIE)
    assert sid and sid != "attacker-chosen-id"
    token = csrf_of(client)
    client.post("/logout", data={"csrf_token": token}, headers=ORIGIN, follow_redirects=False)
    assert app.state.sessions.get(sid) is None


def test_session_idle_timeout():
    now = [1000.0]
    store = SessionStore(idle=60, absolute=3600, clock=lambda: now[0])
    session = store.create(1, "ana", "192.168.1.2", "iPhone")
    now[0] += 59
    assert store.get(session.sid) is not None
    now[0] += 61
    assert store.get(session.sid) is None


def test_limiter_backoff_and_lockout():
    now = [0.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    for _ in range(3):
        limiter.failure("1.1.1.1", "ana")
    assert limiter.retry_after("1.1.1.1", "ana") == 0
    limiter.failure("1.1.1.1", "ana")
    assert limiter.retry_after("1.1.1.1", "ana") == 1
    for _ in range(6):
        limiter.failure("1.1.1.1", "ana")
    assert limiter.retry_after("9.9.9.9", "ANA") >= LoginLimiter.LOCK_SECONDS - 1   # per username too
    limiter.success("1.1.1.1", "ana")
    assert limiter.retry_after("1.1.1.1", "ana") == 0


# ---------------------------------------------------------------------------
# Request hardening
# ---------------------------------------------------------------------------

def test_security_headers_on_every_response(app):
    client = client_for(app)
    for path in ("/login", "/static/css/app.css"):
        headers = client.get(path).headers
        assert "default-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "unsafe-inline" not in headers["content-security-policy"]
        assert headers["strict-transport-security"].startswith("max-age=")
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert "server" not in headers
    assert logged_in(app).get("/").headers["cache-control"] == "no-store"


def test_public_addresses_are_rejected(app):
    client = TestClient(app, base_url=BASE, client=("8.8.8.8", 1234))
    assert client.get("/login").status_code == 403


def test_cross_origin_posts_are_rejected(app):
    client = client_for(app)
    no_origin = client.post("/login", data={"username": "ana", "password": "clave-ana"})
    assert no_origin.status_code == 403
    evil = client.post("/login", data={"username": "ana", "password": "clave-ana"},
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    http_origin = client.post("/login", data={"username": "ana", "password": "clave-ana"},
                              headers={"Origin": "http://testserver"})
    assert http_origin.status_code == 403


def test_null_origin_falls_back_to_referer(app):
    client = client_for(app)
    data = {"username": "ana", "password": "clave-ana"}
    assert client.post("/login", data=data, headers={"Origin": "null"}).status_code == 403
    ok = client.post("/login", data=data, follow_redirects=False,
                     headers={"Origin": "null", "Referer": f"{BASE}/login"})
    assert ok.status_code == 303


def test_writes_require_csrf_token(app, family):
    client = logged_in(app)
    url = f"/movimientos/{family['ana_txn']}/categoria"
    missing = client.post(url, data={"category_id": family["ana_cat"]}, headers=ORIGIN)
    assert missing.status_code == 403
    wrong = client.post(url, data={"category_id": family["ana_cat"]},
                        headers={**ORIGIN, "X-CSRF-Token": "x" * 40})
    assert wrong.status_code == 403
    ok = client.post(url, data={"category_id": family["ana_cat"]},
                     headers={**ORIGIN, "X-CSRF-Token": csrf_of(client)})
    assert ok.status_code == 200


def test_no_docs_or_openapi(app):
    client = logged_in(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---------------------------------------------------------------------------
# Per-user isolation
# ---------------------------------------------------------------------------

def test_user_never_sees_or_edits_another_users_data(app, family, factory):
    client = logged_in(app)                     # ana
    token = csrf_of(client)
    headers = {**ORIGIN, "X-CSRF-Token": token}

    assert "MERCADO BETO" not in client.get("/movimientos").text
    assert client.get(f"/movimientos/{family['beto_txn']}").status_code == 404
    assert client.post(f"/movimientos/{family['beto_txn']}/categoria",
                       data={"category_id": family["beto_cat"]}, headers=headers).status_code == 404
    # Own transaction, but another user's category id.
    assert client.post(f"/movimientos/{family['ana_txn']}/categoria",
                       data={"category_id": family["beto_cat"]}, headers=headers).status_code == 404
    # Investment operation of beto.
    assert client.get(f"/inversiones/operaciones/{family['beto_op']}").status_code == 404
    assert client.post(f"/inversiones/operaciones/{family['beto_op']}/borrar", headers=headers).status_code == 404
    assert "Acme" not in client.get("/inversiones").text

    db = factory()
    try:
        assert db.get(Transaction, family["beto_txn"]).category_id is None
        assert db.get(Transaction, family["ana_txn"]).category_id is None
        assert db.get(InvestmentTransaction, family["beto_op"]) is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pages render
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/", "/?rango=year", "/?rango=all", "/gastos", "/ingresos", "/gastos?periodo=anio",
    "/gastos?periodo=custom&desde=2020-01-01&hasta=2030-01-01", "/movimientos",
    "/movimientos?q=mercado&tipo=expense&categoria=sin", "/movimientos/lista?offset=0",
    "/inversiones", "/inversiones?cerradas=1", "/inversiones/evolucion", "/inversiones/distribucion",
    "/inversiones/distribucion?por=broker", "/inversiones/operaciones", "/inversiones/operaciones/nueva",
    "/inversiones/fiscal", "/mas", "/mas/categorias", "/mas/reglas", "/mas/cuentas", "/mas/importaciones",
    "/mas/categorias/nueva", "/mas/reglas/nueva", "/mas/reglas/probar?pattern=mercado&match_type=contains",
    "/api/resumen", "/api/flujos/gastos", "/api/movimientos", "/api/inversiones/posiciones",
    "/api/inversiones/evolucion", "/manifest.webmanifest", "/sw.js",
])
@pytest.mark.parametrize("user", [("ana", "clave-ana"), ("beto", "clave-beto")])
def test_pages_render(app, path, user):
    client = logged_in(app, *user)
    response = client.get(path)
    assert response.status_code == 200, (path, response.text[:500])


def test_templates_have_no_inline_script_or_style(app):
    client = logged_in(app, "beto", "clave-beto")
    for path in ("/", "/gastos", "/movimientos", "/inversiones", "/inversiones/operaciones/nueva", "/mas",
                 "/mas/categorias", "/mas/reglas", "/mas/reglas/nueva", "/mas/categorias/nueva"):
        html = client.get(path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), path
        assert " style=" not in html and "<style" not in html, path
        assert not re.search(r"\son[a-z]+=", html), path
    assert "<script" not in client.get("/login").text.split("</head>")[1]


def test_tax_xlsx_download(app):
    client = logged_in(app, "beto", "clave-beto")
    year = date.today().year
    response = client.get(f"/inversiones/fiscal/{year}.xlsx")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert client.get("/inversiones/fiscal/1999.xlsx").status_code == 404


# ---------------------------------------------------------------------------
# Light edits
# ---------------------------------------------------------------------------

def test_recategorize_marks_manual_audits_and_notifies(app, family, factory):
    client = logged_in(app)
    response = client.post(f"/movimientos/{family['ana_txn']}/categoria",
                           data={"category_id": family["ana_cat"]},
                           headers={**ORIGIN, "X-CSRF-Token": csrf_of(client), "HX-Request": "true"})
    assert response.status_code == 200
    assert 'hx-swap-oob="outerHTML"' in response.text and "Supermercados" in response.text
    assert "toast" in response.headers["hx-trigger"]
    db = factory()
    try:
        txn = db.get(Transaction, family["ana_txn"])
        assert txn.category_id == family["ana_cat"]
        assert txn.category_source == CATEGORY_SOURCE_MANUAL
        log = db.query(AuditLog).filter(AuditLog.record_id == txn.id).one()
        assert "web 192.168.1.50" in log.new_value
    finally:
        db.close()
    assert app.state.changes == [(family["ana"], "transactions")]


def test_category_sheet_saves_on_tap_and_lists_every_category(app, family, factory):
    db = factory()
    try:
        tipo = Category(user_id=family["ana"], name="INGRESO", level=CategoryLevel.type,
                        transaction_type=TransactionType.income)
        db.add(tipo)
        db.flush()
        db.add(Category(user_id=family["ana"], parent_id=tipo.id, name="Nómina",
                        level=CategoryLevel.category))
        db.commit()
        expected = db.query(Category).filter(Category.user_id == family["ana"],
                                             Category.level == CategoryLevel.category).count()
    finally:
        db.close()

    client = logged_in(app)
    html = client.get(f"/movimientos/{family['ana_txn']}").text
    form = re.search(r"<form[^>]*data-cat-form[^>]*>", html).group(0)
    assert 'hx-trigger="change"' in form                       # saves on tap
    assert 'type="submit"' not in html                         # no button lost below the list
    assert html.count('name="category_id"') == expected + 1    # every category + "Sin categoría"
    assert "Nómina" in html and "Supermercados" in html
    assert html.index(">GASTO <") < html.index(">INGRESO <")   # expense → GASTO first
    assert f"{expected} disponibles" in html
    assert "data-cat-search" in html


def test_investment_operation_create_validate_and_delete(app, family, factory):
    client = logged_in(app, "beto", "clave-beto")
    headers = {**ORIGIN, "X-CSRF-Token": csrf_of(client), "HX-Request": "true"}
    base = {"kind": "sell", "security_id": str(family["security"]), "trade_date": "2025-03-01",
            "broker": "DEGIRO", "price": "12", "amount_eur": "120"}

    too_many = client.post("/inversiones/operaciones", data={**base, "quantity": "50"}, headers=headers)
    assert too_many.status_code == 200 and "alert err" in too_many.text   # ledger refuses it

    ok = client.post("/inversiones/operaciones", data={**base, "quantity": "5", "amount_eur": "60"},
                     headers=headers)
    assert ok.status_code == 204
    assert "refreshPage" in ok.headers["hx-trigger"]

    db = factory()
    try:
        sale = db.query(InvestmentTransaction).filter_by(user_id=family["beto"], kind=InvestmentKind.sell).one()
        sale_id = sale.id
    finally:
        db.close()

    # Deleting the buy would leave the sale without shares → refused.
    refused = client.post(f"/inversiones/operaciones/{family['beto_op']}/borrar", headers=headers)
    assert refused.status_code == 200 and "alert err" in refused.text

    assert client.post(f"/inversiones/operaciones/{sale_id}/borrar", headers=headers).status_code == 204
    db = factory()
    try:
        assert db.get(InvestmentTransaction, sale_id) is None
        assert db.query(AuditLog).filter(AuditLog.table_affected == "investment_transactions").count() == 2
    finally:
        db.close()


def test_future_date_is_refused(app, family):
    client = logged_in(app, "beto", "clave-beto")
    response = client.post("/inversiones/operaciones", data={
        "kind": "buy", "security_id": str(family["security"]), "trade_date": "2999-01-01",
        "broker": "DEGIRO", "quantity": "1", "price": "1", "amount_eur": "1",
    }, headers={**ORIGIN, "X-CSRF-Token": csrf_of(client)})
    assert "La fecha no puede ser futura" in response.text


# ---------------------------------------------------------------------------
# Shared services
# ---------------------------------------------------------------------------

def test_assign_user_transaction_category_checks_ownership(factory, family):
    db = factory()
    try:
        with pytest.raises(LookupError):
            assign_user_transaction_category(db, family["ana"], family["beto_txn"], None)
        with pytest.raises(LookupError):
            assign_user_transaction_category(db, family["ana"], family["ana_txn"], family["beto_cat"])
        assign_user_transaction_category(db, family["ana"], family["ana_txn"], family["ana_cat"])
        assert db.get(Transaction, family["ana_txn"]).category_source == CATEGORY_SOURCE_MANUAL
    finally:
        db.close()


def test_dashboard_summary_matches_desktop_figures(factory, family):
    db = factory()
    try:
        today = date.today()
        s = dashboard_summary(db, family["ana"], date(today.year, today.month, 1), today)
        assert s.balance == 1000.0
        assert s.income == 2000.0 and s.expense == 42.5
        assert s.uncategorized == 2
        assert s.by_category == [("Sin categorizar (1)", 42.5)]
        assert round(s.savings_rate, 4) == round((2000 - 42.5) / 2000 * 100, 4)
        assert [r.issuer_name for r in s.recent][0] in ("MERCADO ANA", "NOMINA")
        assert all("BETO" not in (r.issuer_name or "") for r in s.recent)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Administración: categories and rules (W7)
# ---------------------------------------------------------------------------

def _hx(client: TestClient) -> dict:
    return {**ORIGIN, "X-CSRF-Token": csrf_of(client), "HX-Request": "true"}


def _gasto_type(factory, user_id: int) -> int:
    db = factory()
    try:
        return db.query(Category.id).filter(Category.user_id == user_id, Category.name == "GASTO").scalar()
    finally:
        db.close()


def test_category_create_rename_delete(app, family, factory):
    client = logged_in(app)
    headers = _hx(client)
    gasto = _gasto_type(factory, family["ana"])

    response = client.post("/mas/categorias", data={"parent_id": gasto, "name": "  Farmacia "}, headers=headers)
    assert response.status_code == 204 and "refreshPage" in response.headers["hx-trigger"]
    db = factory()
    try:
        farmacia = db.query(Category).filter(Category.user_id == family["ana"], Category.name == "Farmacia").one()
    finally:
        db.close()

    # Duplicates in the same group are refused with a message in the form.
    response = client.post("/mas/categorias", data={"parent_id": gasto, "name": "farmacia"}, headers=headers)
    assert response.status_code == 200 and "Ya existe" in response.text

    response = client.post(f"/mas/categorias/{farmacia.id}/renombrar", data={"name": "Salud"}, headers=headers)
    assert response.status_code == 204

    # Deleting a used category: its transactions go back to the rules
    # (uncategorized, source cleared) and the rules pointing at it go too.
    db = factory()
    try:
        txn = db.get(Transaction, family["ana_txn"])
        txn.category_id, txn.category_source = farmacia.id, CATEGORY_SOURCE_MANUAL
        db.add(AutoCategorizationRule(user_id=family["ana"], category_id=farmacia.id,
                                      match_pattern="FARMA", match_type=MatchType.contains, priority=100))
        db.commit()
    finally:
        db.close()
    sheet = client.get(f"/mas/categorias/{farmacia.id}").text
    assert "1 movimientos quedarán sin categoría" in sheet and "1 reglas" in sheet
    response = client.post(f"/mas/categorias/{farmacia.id}/borrar", headers=headers)
    assert response.status_code == 204
    db = factory()
    try:
        assert db.get(Category, farmacia.id) is None
        txn = db.get(Transaction, family["ana_txn"])
        assert txn.category_id is None and txn.category_source is None
        assert db.query(AutoCategorizationRule).filter_by(category_id=farmacia.id).count() == 0
        actions = [a.action.value for a in db.query(AuditLog).filter(AuditLog.table_affected == "categories")]
        assert actions == ["insert", "update", "delete"]
    finally:
        db.close()
    assert (family["ana"], "transactions") in app.state.changes

    # Type nodes cannot be deleted.
    response = client.post(f"/mas/categorias/{gasto}/borrar", headers=headers)
    assert response.status_code == 204 and "toastError" in response.headers["hx-trigger"]


def test_categories_and_rules_of_another_user_are_off_limits(app, family, factory):
    db = factory()
    try:
        rule = AutoCategorizationRule(user_id=family["beto"], category_id=family["beto_cat"],
                                      match_pattern="MERCADO", match_type=MatchType.contains, priority=10)
        shared = AutoCategorizationRule(user_id=None, category_id=family["beto_cat"],
                                        match_pattern="SUPER", match_type=MatchType.contains, priority=20)
        db.add_all([rule, shared])
        db.commit()
        rule_id, shared_id = rule.id, shared.id
    finally:
        db.close()

    client = logged_in(app)          # ana
    headers = _hx(client)
    beto_cat = family["beto_cat"]
    assert client.get(f"/mas/categorias/{beto_cat}").status_code == 404
    assert client.post(f"/mas/categorias/{beto_cat}/renombrar", data={"name": "X"}, headers=headers).status_code == 404
    assert client.post(f"/mas/categorias/{beto_cat}/borrar", headers=headers).status_code == 404
    assert client.get(f"/mas/reglas/{rule_id}").status_code == 404
    assert client.post(f"/mas/reglas/{rule_id}/borrar", headers=headers).status_code == 404
    assert client.post("/mas/reglas", data={"rule_id": rule_id, "category_id": family["ana_cat"],
                                            "pattern": "X"}, headers=headers).status_code == 404
    # A rule can only point at one of your own categories.
    response = client.post("/mas/reglas", data={"category_id": beto_cat, "pattern": "X"}, headers=headers)
    assert response.status_code == 200 and "categoría válida" in response.text
    # Global rules are visible but read-only on the web.
    page = client.get("/mas/reglas").text
    assert "«SUPER»" in page and "«MERCADO»" not in page
    response = client.post(f"/mas/reglas/{shared_id}/borrar", headers=headers)
    assert "toastError" in response.headers["hx-trigger"]
    db = factory()
    try:
        assert db.get(Category, beto_cat).name == "Supermercados"
        assert db.get(AutoCategorizationRule, rule_id) is not None
        assert db.get(AutoCategorizationRule, shared_id) is not None
    finally:
        db.close()


def test_rule_crud_priority_preview_and_apply(app, family, factory):
    client = logged_in(app)
    headers = _hx(client)
    response = client.post("/mas/reglas", data={"category_id": family["ana_cat"], "pattern": "mercado",
                                                "match_type": "contains", "priority": "50"}, headers=headers)
    assert response.status_code == 204
    response = client.post("/mas/reglas", data={"category_id": family["ana_cat"], "pattern": "[",
                                                "match_type": "regex"}, headers=headers)
    assert "Expresión regular no válida" in response.text
    db = factory()
    try:
        first = db.query(AutoCategorizationRule).filter_by(user_id=family["ana"]).one()
        assert first.priority == 50 and first.match_pattern == "mercado"
        second = AutoCategorizationRule(user_id=family["ana"], category_id=family["ana_cat"],
                                        match_pattern="NOMINA", match_type=MatchType.exact, priority=60)
        db.add(second)
        db.commit()
        first_id, second_id = first.id, second.id
    finally:
        db.close()

    preview = client.get("/mas/reglas/probar", params={"pattern": "MERCADO", "match_type": "contains"}).text
    assert "1 movimiento coincide" in preview and "MERCADO ANA" in preview and "BETO" not in preview

    assert client.post(f"/mas/reglas/{second_id}/prioridad", data={"direccion": "up"},
                       headers=headers).status_code == 204
    db = factory()
    try:
        assert db.get(AutoCategorizationRule, second_id).priority < db.get(AutoCategorizationRule, first_id).priority
    finally:
        db.close()

    # Apply only to uncategorized: the expense matches «mercado».
    response = client.post("/mas/reglas/aplicar", data={"modo": "sin"}, headers=headers)
    assert response.status_code == 204
    db = factory()
    try:
        txn = db.get(Transaction, family["ana_txn"])
        assert txn.category_id == family["ana_cat"] and txn.category_source == "rule"
        beto_txn = db.get(Transaction, family["beto_txn"])
        assert beto_txn.category_id is None                      # other users untouched
    finally:
        db.close()

    response = client.post(f"/mas/reglas/{first_id}/borrar", headers=headers)
    assert response.status_code == 204
    db = factory()
    try:
        assert db.get(AutoCategorizationRule, first_id) is None
    finally:
        db.close()


def test_apply_all_rules_never_overwrites_manual_categories(factory, family):
    from app.categorization.admin_service import add_category, add_rule, apply_all_rules

    db = factory()
    try:
        gasto = db.query(Category).filter_by(user_id=family["ana"], name="GASTO").one()
        other = add_category(db, family["ana"], gasto.id, "Otros")
        assign_user_transaction_category(db, family["ana"], family["ana_txn"], other.id)
        add_rule(db, family["ana"], family["ana_cat"], "MERCADO")
        apply_all_rules(db, family["ana"], overwrite=True)
        txn = db.get(Transaction, family["ana_txn"])
        assert txn.category_id == other.id and txn.category_source == CATEGORY_SOURCE_MANUAL
    finally:
        db.close()


def test_admin_writes_require_csrf(app, family):
    client = logged_in(app)
    for path, data in (("/mas/categorias", {"parent_id": "1", "name": "X"}),
                       ("/mas/reglas", {"pattern": "X"}), ("/mas/reglas/aplicar", {"modo": "sin"}),
                       ("/mas/categorias/predeterminadas", {})):
        response = client.post(path, data=data, headers=ORIGIN)
        assert response.status_code == 403, path
