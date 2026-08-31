"""
Unit tests for authentication service (Phase 1).

Tests:
  - create_user: happy path, duplicate username, empty inputs
  - verify_login: correct credentials, wrong password, unknown user
  - last_login updated on successful login
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base
from app.auth.auth_service import create_user, verify_login
from app.db.database import ensure_demo_users


# ---------------------------------------------------------------------------
# Fixtures — in-memory SQLite DB per test
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Provide a fresh in-memory SQLite session for each test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# create_user tests
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_create_user_happy_path(self, db_session):
        user = create_user(db_session, "pedro", "secret123")
        assert user.id is not None
        assert user.username == "pedro"
        assert user.password_hash != "secret123"  # never store plaintext
        assert user.password_hash.startswith("$2b$")  # bcrypt prefix

    def test_create_user_duplicate_raises(self, db_session):
        create_user(db_session, "pedro", "secret123")
        with pytest.raises(ValueError, match="ya existe"):
            create_user(db_session, "pedro", "otherpassword")

    def test_create_user_empty_username_raises(self, db_session):
        with pytest.raises(ValueError, match="vacío"):
            create_user(db_session, "   ", "secret123")

    def test_create_user_empty_password_raises(self, db_session):
        with pytest.raises(ValueError, match="vacía"):
            create_user(db_session, "pedro", "")


# ---------------------------------------------------------------------------
# verify_login tests
# ---------------------------------------------------------------------------

class TestVerifyLogin:
    def test_login_correct_credentials(self, db_session):
        create_user(db_session, "maria", "mypassword")
        user = verify_login(db_session, "maria", "mypassword")
        assert user is not None
        assert user.username == "maria"

    def test_login_wrong_password_returns_none(self, db_session):
        create_user(db_session, "maria", "mypassword")
        result = verify_login(db_session, "maria", "wrongpassword")
        assert result is None

    def test_login_unknown_user_returns_none(self, db_session):
        result = verify_login(db_session, "fantasma", "anything")
        assert result is None

    def test_login_updates_last_login(self, db_session):
        create_user(db_session, "comun", "pass123")
        user = verify_login(db_session, "comun", "pass123")
        assert user.last_login is not None


def test_ensure_demo_users_does_not_create_automatic_credentials(db_session):
    created = ensure_demo_users(db_session)

    assert created == []
    assert verify_login(db_session, "usuario", "123456") is None
    assert verify_login(db_session, "admin", "123456") is None