"""
tests/test_rules_engine.py
===========================
Unit tests for the auto-categorization rules engine (Phase 3).

Covers:
  - R-01: No rules defined → all transactions unmatched
  - R-02: Single contains rule → matched via issuer_name
  - R-03: Single contains rule → matched via concept (OR gate)
  - R-04: Exact match rule
  - R-05: Regex match rule
  - R-06: Priority — lower number wins over higher number
  - R-07: Conflict — two rules same priority, different categories
  - R-08: Global rule applies when user_id=None
  - R-09: apply_rules_and_commit persists assignments
  - R-10: No match → unmatched list
  - R-20..R-22: rules limited to a movement type (gasto / ingreso)
"""
from __future__ import annotations

import pytest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, Transaction, TransactionType,
    Account, Category, CategoryLevel, AutoCategorizationRule, MatchType,
)
from app.auth.auth_service import create_user
from app.categorization.rules_engine import (
    apply_rules,
    apply_rules_and_commit,
    create_rule,
    RulesEngineResult,
)
from app.categorization.category_manager import seed_default_categories


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
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
def base_data(db_session):
    """Create user, account, and two categories (GASTO + INGRESO type nodes)."""
    user = create_user(db_session, "pedro_test", "clave-de-prueba")
    account = Account(
        user_id      = user.id,
        account_name = "CaixaBank Principal",
        iban_last4   = "7363",
    )
    db_session.add(account)
    db_session.flush()

    # Type nodes
    type_gasto = Category(
        user_id          = user.id,
        name             = "GASTO",
        level            = CategoryLevel.type,
        transaction_type = TransactionType.expense,
    )
    type_ingreso = Category(
        user_id          = user.id,
        name             = "INGRESO",
        level            = CategoryLevel.type,
        transaction_type = TransactionType.income,
    )
    db_session.add_all([type_gasto, type_ingreso])
    db_session.flush()

    # Category nodes
    cat_super = Category(
        user_id   = user.id,
        parent_id = type_gasto.id,
        name      = "Supermercados",
        level     = CategoryLevel.category,
    )
    cat_nomina = Category(
        user_id   = user.id,
        parent_id = type_ingreso.id,
        name      = "Nomina Pedro",
        level     = CategoryLevel.category,
    )
    db_session.add_all([cat_super, cat_nomina])
    db_session.commit()

    return {
        "user_id":    user.id,
        "account_id": account.id,
        "cat_super":  cat_super,
        "cat_nomina": cat_nomina,
    }


def _make_txn(db_session, user_id, account_id, issuer=None, concept=None,
              amount=-50.0) -> Transaction:
    """Helper: create and persist a minimal Transaction."""
    txn = Transaction(
        user_id          = user_id,
        account_id       = account_id,
        transaction_date = date(2025, 6, 1),
        amount           = amount,
        type             = TransactionType.expense if amount < 0 else TransactionType.income,
        issuer_name      = issuer,
        concept          = concept,
        source_file      = "test.xlsx",
    )
    db_session.add(txn)
    db_session.commit()
    return txn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRulesEngine:

    def test_r01_no_rules_all_unmatched(self, db_session, base_data):
        """R-01: No rules defined → all transactions land in unmatched."""
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="MERCADONA")
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert txn.id in result.unmatched
        assert result.assigned == {}
        assert result.conflicts == {}

    def test_r02_contains_match_issuer(self, db_session, base_data):
        """R-02: contains rule matches via issuer_name."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="MERCADONA SA")
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert result.assigned[txn.id] == base_data["cat_super"].id
        rule = db_session.query(AutoCategorizationRule).one()
        assert result.assigned_by_rule == {rule.id: 1}
        assert result.transaction_ids_by_rule == {rule.id: [txn.id]}
        assert txn.id not in result.unmatched

    def test_r03_contains_match_concept_or_gate(self, db_session, base_data):
        """R-03: contains rule matches via concept when issuer_name is None."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "supermercado",
            match_type    = MatchType.contains,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"],
                        issuer=None,
                        concept="COMPRA SUPERMERCADO ONLINE")
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert result.assigned[txn.id] == base_data["cat_super"].id

    def test_r04_exact_match(self, db_session, base_data):
        """R-04: exact match rule — only full-string match triggers."""
        create_rule(
            db_session,
            category_id   = base_data["cat_nomina"].id,
            match_pattern = "NOMINA PEDRO",
            match_type    = MatchType.exact,
        )
        txn_match = _make_txn(db_session, base_data["user_id"],
                              base_data["account_id"],
                              issuer="NOMINA PEDRO", amount=2000.0)
        txn_no_match = _make_txn(db_session, base_data["user_id"],
                                 base_data["account_id"],
                                 issuer="NOMINA PEDRO SA", amount=2000.0)
        result = apply_rules(
            db_session,
            [txn_match.id, txn_no_match.id],
            base_data["user_id"]
        )
        assert result.assigned[txn_match.id] == base_data["cat_nomina"].id
        assert txn_no_match.id in result.unmatched

    def test_r05_regex_match(self, db_session, base_data):
        """R-05: regex rule matches using re.search."""
        create_rule(
            db_session,
            category_id   = base_data["cat_nomina"].id,
            match_pattern = r"N[OÓ]MINA\s+PEDRO",
            match_type    = MatchType.regex,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"],
                        issuer="NÓMINA PEDRO 2025", amount=2000.0)
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert result.assigned[txn.id] == base_data["cat_nomina"].id

    def test_r06_priority_lower_wins(self, db_session, base_data):
        """R-06: rule with lower priority number wins over higher number."""
        # Higher priority (lower number) → Supermercados
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
            priority      = 10,
        )
        # Lower priority (higher number) → Nomina (should NOT win)
        create_rule(
            db_session,
            category_id   = base_data["cat_nomina"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
            priority      = 50,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="MERCADONA")
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert result.assigned[txn.id] == base_data["cat_super"].id
        rules = db_session.query(AutoCategorizationRule).order_by(
            AutoCategorizationRule.priority
        ).all()
        assert result.assigned_by_rule == {rules[0].id: 1}

    def test_r07_conflict_same_priority_different_categories(
        self, db_session, base_data
    ):
        """R-07: two rules at same priority, different categories → conflict."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "AMAZON",
            match_type    = MatchType.contains,
            priority      = 100,
        )
        create_rule(
            db_session,
            category_id   = base_data["cat_nomina"].id,
            match_pattern = "AMAZON",
            match_type    = MatchType.contains,
            priority      = 100,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="AMAZON EU")
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert txn.id in result.conflicts
        assert txn.id not in result.assigned

    def test_r08_global_rule_user_id_none(self, db_session, base_data):
        """R-08: global rule (user_id=None) applies to any user."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "LIDL",
            match_type    = MatchType.contains,
            user_id       = None,  # global
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="LIDL ESPAÑA")
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert result.assigned[txn.id] == base_data["cat_super"].id

    def test_r09_apply_and_commit_persists(self, db_session, base_data):
        """R-09: apply_rules_and_commit writes category_id to DB."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "CARREFOUR",
            match_type    = MatchType.contains,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="CARREFOUR")
        assert txn.category_id is None  # before

        apply_rules_and_commit(db_session, [txn.id], base_data["user_id"])

        db_session.refresh(txn)
        assert txn.category_id == base_data["cat_super"].id  # after

    def test_r10_unmatched_no_category_assigned(self, db_session, base_data):
        """R-10: unmatched transaction keeps category_id = None."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="REPSOL")
        result = apply_rules(db_session, [txn.id], base_data["user_id"])
        assert txn.id in result.unmatched
        assert txn.category_id is None

    def test_r11_no_overwrite_preserves_manual_category(self, db_session, base_data):
        """R-11: apply_rules_and_commit(overwrite=False) must not touch a
        transaction that a user already categorized manually, even if a
        rule matches it — this is the passive-refresh/tab-visit path used
        by TransactionsView, which must never silently revert a manual fix."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="MERCADONA")
        # User manually assigns a different category than the rule would.
        txn.category_id = base_data["cat_nomina"].id
        db_session.commit()

        apply_rules_and_commit(
            db_session, [txn.id], base_data["user_id"], overwrite=False
        )

        db_session.refresh(txn)
        assert txn.category_id == base_data["cat_nomina"].id

    def test_r12_apply_and_commit_persists_multiple_assignments(
        self, db_session, base_data
    ):
        """R-12: apply_rules_and_commit's write-back loop must persist
        category_id for every assigned transaction, not just a single one."""
        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
        )
        txns = [
            _make_txn(db_session, base_data["user_id"],
                      base_data["account_id"], issuer="MERCADONA")
            for _ in range(5)
        ]
        assert all(txn.category_id is None for txn in txns)

        result = apply_rules_and_commit(
            db_session, [t.id for t in txns], base_data["user_id"]
        )

        assert len(result.assigned) == 5
        for txn in txns:
            db_session.refresh(txn)
            assert txn.category_id == base_data["cat_super"].id
    def test_r13_overwrite_never_touches_manual_category(self, db_session, base_data):
        """R-13: the explicit "re-apply all rules" action (overwrite=True)
        must still keep a category a family member chose by hand."""
        from app.categorization.category_manager import assign_transaction_category
        from app.db.models import CATEGORY_SOURCE_MANUAL

        create_rule(
            db_session,
            category_id   = base_data["cat_super"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="MERCADONA")
        assign_transaction_category(db_session, txn.id, base_data["cat_nomina"].id)

        apply_rules_and_commit(
            db_session, [txn.id], base_data["user_id"], overwrite=True
        )

        db_session.refresh(txn)
        assert txn.category_id == base_data["cat_nomina"].id
        assert txn.category_source == CATEGORY_SOURCE_MANUAL

    def test_r14_overwrite_updates_rule_assigned_category(self, db_session, base_data):
        """R-14: overwrite=True still re-categorizes rows a rule set earlier,
        and marks every rule assignment with category_source='rule'."""
        from app.db.models import CATEGORY_SOURCE_RULE

        rule = create_rule(
            db_session,
            category_id   = base_data["cat_nomina"].id,
            match_pattern = "MERCADONA",
            match_type    = MatchType.contains,
        )
        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="MERCADONA")
        apply_rules_and_commit(db_session, [txn.id], base_data["user_id"])
        db_session.refresh(txn)
        assert txn.category_source == CATEGORY_SOURCE_RULE

        rule.category_id = base_data["cat_super"].id
        db_session.commit()
        apply_rules_and_commit(
            db_session, [txn.id], base_data["user_id"], overwrite=True
        )

        db_session.refresh(txn)
        assert txn.category_id == base_data["cat_super"].id
        assert txn.category_source == CATEGORY_SOURCE_RULE

    def test_r15_clearing_category_hands_back_to_rules(self, db_session, base_data):
        """R-15: clearing a category manually removes the manual protection."""
        from app.categorization.category_manager import assign_transaction_category

        txn = _make_txn(db_session, base_data["user_id"],
                        base_data["account_id"], issuer="MERCADONA")
        assign_transaction_category(db_session, txn.id, base_data["cat_nomina"].id)
        assign_transaction_category(db_session, txn.id, None)

        db_session.refresh(txn)
        assert txn.category_id is None
        assert txn.category_source is None


def _second_user(db_session, with_super: bool, username: str = "maria_test"):
    """Another family member with an optional own 'GASTO > Supermercados'."""
    user = create_user(db_session, username, "clave-de-prueba", enforce_policy=False)  # may be the DEMO account
    account = Account(user_id=user.id, account_name="Cuenta María")
    db_session.add(account)
    db_session.flush()
    own_super = None
    if with_super:
        gasto = Category(user_id=user.id, name="GASTO", level=CategoryLevel.type,
                         transaction_type=TransactionType.expense)
        db_session.add(gasto)
        db_session.flush()
        own_super = Category(user_id=user.id, parent_id=gasto.id,
                             name="Supermercados", level=CategoryLevel.category)
        db_session.add(own_super)
    db_session.commit()
    return user.id, account.id, own_super


def test_r16_global_rule_maps_to_other_users_equivalent_category(db_session, base_data):
    """R-16: a global rule assigns the other user's own same-named category."""
    create_rule(db_session, category_id=base_data["cat_super"].id,
                match_pattern="LIDL", user_id=None)
    user_id, account_id, own_super = _second_user(db_session, with_super=True)
    txn = _make_txn(db_session, user_id, account_id, issuer="LIDL")

    result = apply_rules_and_commit(db_session, [txn.id], user_id)

    assert result.assigned[txn.id] == own_super.id
    db_session.refresh(txn)
    assert txn.category_id == own_super.id


def test_r17_global_rule_skipped_without_equivalent_category(db_session, base_data):
    """R-17: never assign another user's category id."""
    create_rule(db_session, category_id=base_data["cat_super"].id,
                match_pattern="LIDL", user_id=None)
    user_id, account_id, _ = _second_user(db_session, with_super=False)
    txn = _make_txn(db_session, user_id, account_id, issuer="LIDL")

    result = apply_rules(db_session, [txn.id], user_id)

    assert result.assigned == {}
    assert result.unmatched == [txn.id]


def test_r18_other_users_personal_rule_never_applies(db_session, base_data):
    """R-18: a per-user rule is not loaded for a different user."""
    create_rule(db_session, category_id=base_data["cat_super"].id,
                match_pattern="LIDL", user_id=base_data["user_id"])
    user_id, account_id, _ = _second_user(db_session, with_super=True)
    txn = _make_txn(db_session, user_id, account_id, issuer="LIDL")

    assert apply_rules(db_session, [txn.id], user_id).assigned == {}


def test_r19_demo_account_never_sees_or_gets_global_rules(db_session, base_data):
    """R-19: the DEMO account (shown to visitors) is kept apart from the
    family's global rules: not listed, not applied."""
    from app.categorization.rules_engine import get_all_rules, shares_global_rules

    create_rule(db_session, category_id=base_data["cat_super"].id,
                match_pattern="LIDL", user_id=None)
    user_id, account_id, _ = _second_user(db_session, with_super=True, username="demo")
    txn = _make_txn(db_session, user_id, account_id, issuer="LIDL")

    assert not shares_global_rules(db_session, user_id)
    assert shares_global_rules(db_session, base_data["user_id"])
    assert get_all_rules(db_session, user_id=user_id) == []
    assert apply_rules(db_session, [txn.id], user_id).assigned == {}


def test_r20_same_text_paid_and_received_goes_to_two_categories(db_session, base_data):
    """R-20: a BIZUM paid and a BIZUM received each follow the rule of their
    own movement type; equal pattern and priority is not a conflict."""
    uid, acc = base_data["user_id"], base_data["account_id"]
    paid = _make_txn(db_session, uid, acc, concept="BIZUM A JUAN", amount=-20.0)
    received = _make_txn(db_session, uid, acc, concept="BIZUM DE JUAN", amount=20.0)
    create_rule(db_session, base_data["cat_super"].id, "BIZUM", user_id=uid,
                transaction_type=TransactionType.expense)
    create_rule(db_session, base_data["cat_nomina"].id, "BIZUM", user_id=uid,
                transaction_type=TransactionType.income)

    result = apply_rules(db_session, [paid.id, received.id], uid)

    assert result.assigned == {paid.id: base_data["cat_super"].id,
                               received.id: base_data["cat_nomina"].id}
    assert result.conflicts == {}


def test_r21_rule_without_movement_type_matches_both(db_session, base_data):
    """R-21: existing rules (transaction_type NULL) keep matching any type."""
    uid, acc = base_data["user_id"], base_data["account_id"]
    paid = _make_txn(db_session, uid, acc, concept="BIZUM A JUAN", amount=-20.0)
    received = _make_txn(db_session, uid, acc, concept="BIZUM DE JUAN", amount=20.0)
    rule = create_rule(db_session, base_data["cat_super"].id, "BIZUM", user_id=uid)

    assert rule.transaction_type is None
    result = apply_rules(db_session, [paid.id, received.id], uid)
    assert set(result.assigned) == {paid.id, received.id}


def test_r22_movement_type_validation_and_update(db_session, base_data):
    """R-22: only gasto / ingreso / any; update_rule keeps the type unless
    it is passed, and None sets it back to any."""
    from app.categorization.rules_engine import update_rule

    with pytest.raises(ValueError):
        create_rule(db_session, base_data["cat_super"].id, "TRESSIS",
                    transaction_type=TransactionType.investment)
    rule = create_rule(db_session, base_data["cat_super"].id, "BIZUM",
                       transaction_type=TransactionType.expense)

    assert update_rule(db_session, rule.id, priority=5).transaction_type == TransactionType.expense
    assert update_rule(db_session, rule.id, transaction_type="income").transaction_type == TransactionType.income
    assert update_rule(db_session, rule.id, transaction_type=None).transaction_type is None
