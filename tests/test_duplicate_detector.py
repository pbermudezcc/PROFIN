"""
tests/test_duplicate_detector.py
==================================
Unit tests for duplicate detection logic (Phase 2).

Covers:
  - D-1: exact 4-field match → auto_skip
  - D-1 intra-batch: exact duplicate within the same file → auto_skip
  - D-2: same date+concepto+amount, different balance → clean (not flagged)
  - D-3: intra-batch partial match with different balance → needs_review
  - Completely clean batch → all rows in clean
"""
from __future__ import annotations

import pytest
from datetime import date

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Transaction, TransactionType, Account, User
from app.auth.auth_service import create_user
from app.ingestion.duplicate_detector import check_duplicates


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """In-memory SQLite session with schema created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def user_and_account(db_session):
    """Create a user + account and return (user_id, account_id)."""
    user = create_user(db_session, "pedro_test", "password123")
    account = Account(
        user_id=user.id,
        account_name="CaixaBank Principal",
        iban_last4="7363",
    )
    db_session.add(account)
    db_session.commit()
    return user.id, account.id


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal DataFrame for duplicate detection tests."""
    defaults = {
        "transaction_date": date(2025, 6, 1),
        "value_date":       date(2025, 6, 1),
        "amount":           -50.0,
        "running_balance":  1000.0,
        "concepto_comun":   "044",
        "concepto_propio":  "000",
        "issuer_name":      "MERCADONA",
        "concept":          "MERCADONA | TARJETA CREDITO",
        "raw_col14":        "MERCADONA",
        "currency":         "EUR",
        "iban_raw":         "2100 0732 21 0102117363",
    }
    return pd.DataFrame([{**defaults, **r} for r in rows])


def _add_existing_txn(
    session, user_id, account_id,
    txn_date, concepto_comun, amount, running_balance
) -> None:
    """Insert one existing Transaction into DB for testing."""
    txn = Transaction(
        user_id          = user_id,
        account_id       = account_id,
        transaction_date = txn_date,
        value_date       = txn_date,
        amount           = amount,
        running_balance  = running_balance,
        type             = TransactionType.expense,
        concepto_comun   = concepto_comun,
        source_file      = "test_existing.xlsx",
    )
    session.add(txn)
    session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDuplicateDetector:

    def test_clean_batch_no_existing(self, db_session, user_and_account):
        """All rows are new — should all land in clean."""
        user_id, account_id = user_and_account
        df = _make_df([
            {"amount": -50.0,  "running_balance": 950.0},
            {"amount": -100.0, "running_balance": 850.0,
             "transaction_date": date(2025, 6, 2)},
        ])
        result = check_duplicates(df, db_session, user_id, account_id)
        assert result.auto_skip == []
        assert result.needs_review == []
        assert len(result.clean) == 2

    def test_d1_exact_match_db(self, db_session, user_and_account):
        """D-1: row matches existing DB record on all 4 fields → auto_skip."""
        user_id, account_id = user_and_account
        _add_existing_txn(
            db_session, user_id, account_id,
            txn_date        = date(2025, 6, 1),
            concepto_comun  = "044",
            amount          = -50.0,
            running_balance = 950.0,
        )
        df = _make_df([{"amount": -50.0, "running_balance": 950.0}])
        result = check_duplicates(df, db_session, user_id, account_id)
        assert 0 in result.auto_skip
        assert result.clean == []
        assert "Duplicado exacto en BD" in result.skip_reasons[0]

    def test_d1_intra_batch_duplicate(self, db_session, user_and_account):
        """D-1 intra-batch: two identical rows in same file → second auto_skip."""
        user_id, account_id = user_and_account
        df = _make_df([
            {"amount": -50.0, "running_balance": 950.0},
            {"amount": -50.0, "running_balance": 950.0},  # exact copy
        ])
        result = check_duplicates(df, db_session, user_id, account_id)
        assert 0 in result.clean    # first occurrence: clean
        assert 1 in result.auto_skip  # second: intra-batch duplicate
        assert "dentro del fichero" in result.skip_reasons[1]

    def test_d2_same_fields_different_balance_not_flagged(
        self, db_session, user_and_account
    ):
        """
        D-2: same date+concepto+amount but different running_balance
        vs DB → NOT a duplicate, import as clean.
        """
        user_id, account_id = user_and_account
        _add_existing_txn(
            db_session, user_id, account_id,
            txn_date        = date(2025, 6, 1),
            concepto_comun  = "044",
            amount          = -50.0,
            running_balance = 950.0,  # different from incoming
        )
        df = _make_df([{"amount": -50.0, "running_balance": 1200.0}])
        result = check_duplicates(df, db_session, user_id, account_id)
        assert 0 in result.clean
        assert result.auto_skip == []
        assert result.needs_review == []

    def test_d3_intra_batch_partial_diff_balance(
        self, db_session, user_and_account
    ):
        """
        D-3: two rows in same file with same date+concepto+amount but
        different running_balance → second row flagged for review.
        """
        user_id, account_id = user_and_account
        df = _make_df([
            {"amount": -50.0, "running_balance": 950.0},
            {"amount": -50.0, "running_balance": 900.0},  # same partial, diff balance
        ])
        result = check_duplicates(df, db_session, user_id, account_id)
        assert 0 in result.clean
        assert 1 in result.needs_review
        assert "Posible duplicado" in result.review_reasons[1]

    def test_mixed_batch(self, db_session, user_and_account):
        """Mixed batch: clean + auto_skip + needs_review in one call."""
        user_id, account_id = user_and_account
        # Pre-seed one exact match
        _add_existing_txn(
            db_session, user_id, account_id,
            txn_date        = date(2025, 6, 1),
            concepto_comun  = "044",
            amount          = -50.0,
            running_balance = 950.0,
        )
        df = _make_df([
            # row 0: clean (different date)
            {"transaction_date": date(2025, 7, 1),
             "amount": -75.0, "running_balance": 800.0},
            # row 1: D-1 exact match DB → auto_skip
            {"amount": -50.0, "running_balance": 950.0},
            # row 2: same partial as row 0 (same date+conc+amount), diff balance
            {"transaction_date": date(2025, 7, 1),
             "amount": -75.0, "running_balance": 750.0},
        ])
        result = check_duplicates(df, db_session, user_id, account_id)
        assert 0 in result.clean
        assert 1 in result.auto_skip
        assert 2 in result.needs_review