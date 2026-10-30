"""
SQLAlchemy ORM models for FAMILYFIN.

Hierarchy:
  users
    └── accounts          (one per user in v1, multi-account ready)
    └── categories        (self-referencing tree: type → category → subcategory)
    └── import_batches
          └── transactions
    └── auto_categorization_rules
    └── audit_log
    └── investment_transactions ──> securities (shared catalogue)
                                      └── price_history
  fx_rates (shared, per currency and day)
"""

from __future__ import annotations

import enum
from datetime import datetime, date
from typing import Optional, List

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class TransactionType(str, enum.Enum):
    income = "income"
    expense = "expense"
    investment = "investment"
    unknown = "unknown"


class CategoryLevel(str, enum.Enum):
    type = "type"
    category = "category"
    subcategory = "subcategory"


class MatchType(str, enum.Enum):
    contains = "contains"
    regex = "regex"
    exact = "exact"


class AuditAction(str, enum.Enum):
    insert = "insert"
    update = "update"
    delete = "delete"


class ImportStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    partial = "partial"


class AssetType(str, enum.Enum):
    stock = "stock"
    etf = "etf"
    fund = "fund"


class InvestmentKind(str, enum.Enum):
    buy = "buy"
    sell = "sell"
    dividend = "dividend"
    split = "split"
    grant = "grant"     # shares received from an employer plan (RSU / ESPP / matching)


# Security.price_source / PriceHistory.source values (plain strings).
PRICE_SOURCE_YAHOO  = "yahoo"    # refreshed from the market-data provider
PRICE_SOURCE_MANUAL = "manual"   # typed in by a family member (e.g. a fund NAV)


# Transaction.category_source values. Stored as plain strings (not an Enum
# column) so the column can be added to existing SQLite files with ALTER TABLE.
CATEGORY_SOURCE_MANUAL = "manual"   # set by a family member — rules never touch it
CATEGORY_SOURCE_RULE   = "rule"     # set by the auto-categorization rules engine


# Category.nature values (plain strings, added by migration 9). NULL = variable.
CATEGORY_NATURE_FIXED    = "fijo"      # recurring bill or income (hipoteca, luz, nómina…)
CATEGORY_NATURE_TRANSFER = "traspaso"  # money moved between family members (aportes)
CATEGORY_NATURES = (CATEGORY_NATURE_FIXED, CATEGORY_NATURE_TRANSFER)

# Transaction.fixed_override values. NULL = follow the category's nature.
FIXED_OVERRIDE_FIXED    = "fijo"
FIXED_OVERRIDE_VARIABLE = "variable"
FIXED_OVERRIDES = (FIXED_OVERRIDE_FIXED, FIXED_OVERRIDE_VARIABLE)


def effective_nature(fixed_override: Optional[str], category_nature: Optional[str]) -> Optional[str]:
    """The nature a movement counts with: CATEGORY_NATURE_* or None (variable).

    A traspaso category always wins (an aporte is never a bill); otherwise the
    movement's own override beats its category's default.
    """
    if category_nature == CATEGORY_NATURE_TRANSFER:
        return CATEGORY_NATURE_TRANSFER
    if fixed_override == FIXED_OVERRIDE_FIXED:
        return CATEGORY_NATURE_FIXED
    if fixed_override == FIXED_OVERRIDE_VARIABLE:
        return None
    return category_nature if category_nature in CATEGORY_NATURES else None


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # bcrypt embeds the salt in the hash — stored for reference/auditing
    salt: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    accounts: Mapped[List[Account]] = relationship(
        "Account", back_populates="user", cascade="all, delete-orphan"
    )
    categories: Mapped[List[Category]] = relationship(
        "Category", back_populates="user", cascade="all, delete-orphan",
        foreign_keys="Category.user_id",
    )
    rules: Mapped[List[AutoCategorizationRule]] = relationship(
        "AutoCategorizationRule", back_populates="user", cascade="all, delete-orphan"
    )
    import_batches: Mapped[List[ImportBatch]] = relationship(
        "ImportBatch", back_populates="user", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[List[AuditLog]] = relationship(
        "AuditLog", back_populates="user", cascade="all, delete-orphan"
    )
    investment_transactions: Mapped[List[InvestmentTransaction]] = relationship(
        "InvestmentTransaction", back_populates="user", cascade="all, delete-orphan"
    )
    broker_cash: Mapped[List[BrokerCash]] = relationship(
        "BrokerCash", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    account_name: Mapped[str] = mapped_column(String(128), nullable=False)
    iban_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    bank: Mapped[str] = mapped_column(
        String(64), nullable=False, default="CaixaBank"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="accounts")
    transactions: Mapped[List[Transaction]] = relationship(
        "Transaction", back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Account id={self.id} name={self.account_name!r} user_id={self.user_id}>"


# ---------------------------------------------------------------------------
# categories  (self-referencing tree)
# ---------------------------------------------------------------------------
class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    level: Mapped[CategoryLevel] = mapped_column(
        Enum(CategoryLevel), nullable=False
    )
    transaction_type: Mapped[Optional[TransactionType]] = mapped_column(
        Enum(TransactionType), nullable=True
    )
    # CATEGORY_NATURE_* or NULL (variable). Its movements are fixed unless
    # they override it; traspaso is left out of the forecast. Migration 9.
    nature: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # For a traspaso category: the family member the money goes to or comes
    # from (e.g. COMUN's «Aporte Comun María» → MARIA). Migration 9.
    transfer_peer_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped[User] = relationship(
        "User", back_populates="categories", foreign_keys=[user_id]
    )
    parent: Mapped[Optional[Category]] = relationship(
        "Category", remote_side="Category.id", back_populates="children"
    )
    children: Mapped[List[Category]] = relationship(
        "Category", back_populates="parent", cascade="all, delete-orphan"
    )
    transactions: Mapped[List[Transaction]] = relationship(
        "Transaction", back_populates="category",
        passive_deletes=True,
    )
    rules: Mapped[List[AutoCategorizationRule]] = relationship(
        "AutoCategorizationRule", back_populates="category",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Category id={self.id} name={self.name!r} level={self.level}>"


# ---------------------------------------------------------------------------
# import_batches
# ---------------------------------------------------------------------------
class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    import_timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    rows_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus), nullable=False, default=ImportStatus.pending
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="import_batches")
    transactions: Mapped[List[Transaction]] = relationship(
        "Transaction", back_populates="import_batch"
    )

    def __repr__(self) -> str:
        return (
            f"<ImportBatch id={self.id} file={self.filename!r} "
            f"status={self.status} rows={self.rows_imported}>"
        )


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------
class Transaction(Base):
    """
    Central fact table. One row = one bank movement.

    Raw CaixaBank fields (all persisted for traceability):
      transaction_date  ← Fecha (Col 0)
      value_date        ← Fecha valor (Col 1)
      amount            ← unified: positive=income, negative=expense (Col 6 or 7)
      running_balance   ← Saldo (Col 8 or 9 — confirm on first real import)
      concepto_comun    ← Concepto común code (Col 10-ish)
      concepto_propio   ← Concepto propio code
      issuer_name       ← Extracted from Col 14 (date + issuer pattern)
      concept           ← Concatenation of Cols 15–21

    Derived fields:
      type              ← Derived from amount sign + rule overrides
      category_id       ← Auto-assigned by rules or manual
      category_source   ← 'manual' | 'rule' | NULL (who set category_id)
    """

    __tablename__ = "transactions"
    # Every view filters by user and sorts/filters by date or category, and
    # duplicate detection looks up (user, account, date). Existing files get
    # these through schema migration v2 (same names, IF NOT EXISTS).
    __table_args__ = (
        Index("ix_transactions_user_date", "user_id", "transaction_date"),
        Index("ix_transactions_user_category", "user_id", "category_id"),
        Index("ix_transactions_user_account_date", "user_id", "account_id", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    import_batch_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    # Who set category_id: CATEGORY_SOURCE_MANUAL | CATEGORY_SOURCE_RULE | None
    category_source: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    # FIXED_OVERRIDE_* set by hand, or NULL = the category's nature decides.
    # Added by migration 9.
    fixed_override: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True
    )

    # --- Core date & amount fields ---
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    value_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    running_balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- Derived / enriched fields ---
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType), nullable=False, default=TransactionType.unknown
    )
    issuer_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    concept: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Raw CaixaBank metadata fields (all stored for future use) ---
    concepto_comun: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    concepto_propio: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Raw concatenation of cols 15–21 before cleaning
    raw_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Col 14 raw value (date + issuer string as received from bank)
    raw_col14: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Import tracking ---
    source_file: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # Position in the bank's chronological order (higher = later). The date
    # alone cannot order movements of the same day, and CaixaBank exports
    # list them newest first, so the import id is the reverse of time within
    # a statement. Set by the importer; filled for older rows by migration 7.
    bank_order: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_duplicate_flagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    duplicate_resolved_by_user: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    account: Mapped[Account] = relationship("Account", back_populates="transactions")
    import_batch: Mapped[Optional[ImportBatch]] = relationship(
        "ImportBatch", back_populates="transactions"
    )
    category: Mapped[Optional[Category]] = relationship(
        "Category", back_populates="transactions"
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id} date={self.transaction_date} "
            f"amount={self.amount} type={self.type}>"
        )


# Newest movement first: by date, then by the bank's order within the day.
# Every list and every "latest balance" uses it.
TRANSACTIONS_NEWEST_FIRST = (
    Transaction.transaction_date.desc(),
    Transaction.bank_order.desc(),
    Transaction.id.desc(),
)


# ---------------------------------------------------------------------------
# auto_categorization_rules
# ---------------------------------------------------------------------------
class AutoCategorizationRule(Base):
    __tablename__ = "auto_categorization_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    match_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    match_type: Mapped[MatchType] = mapped_column(
        Enum(MatchType), nullable=False, default=MatchType.contains
    )
    # Lower number = higher priority
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Only movements of this type (Transaction.type: income / expense) match.
    # NULL = any type. The same text (e.g. «BIZUM») can be a payment or a
    # receipt, and each usually needs its own category. Added by migration 8.
    transaction_type: Mapped[Optional[TransactionType]] = mapped_column(
        Enum(TransactionType), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped[Optional[User]] = relationship("User", back_populates="rules")
    category: Mapped[Category] = relationship("Category", back_populates="rules")

    def __repr__(self) -> str:
        return (
            f"<AutoCategorizationRule id={self.id} pattern={self.match_pattern!r} "
            f"type={self.match_type}>"
        )


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    table_affected: Mapped[str] = mapped_column(String(64), nullable=False)
    record_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction), nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped[Optional[User]] = relationship("User", back_populates="audit_logs")

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} table={self.table_affected!r} "
            f"action={self.action} record_id={self.record_id}>"
        )


# ---------------------------------------------------------------------------
# securities  (shared catalogue: one row per market-data symbol)
# ---------------------------------------------------------------------------
class Security(Base):
    """A listed instrument. Shared by every user; holdings live in the trades.

    `symbol` is the market-data provider's symbol, which also fixes the
    listing: AMZN (NASDAQ, USD) and AMZ.DE (Xetra, EUR) are two securities.
    """

    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(
        Enum(AssetType), nullable=False, default=AssetType.stock
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    isin: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # PRICE_SOURCE_YAHOO | PRICE_SOURCE_MANUAL
    price_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PRICE_SOURCE_YAHOO
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # passive_deletes="all": deleting a security that still has trades must
    # fail on the RESTRICT foreign key, never orphan or null the user's ledger.
    transactions: Mapped[List[InvestmentTransaction]] = relationship(
        "InvestmentTransaction", back_populates="security", passive_deletes="all"
    )
    prices: Mapped[List[PriceHistory]] = relationship(
        "PriceHistory", back_populates="security",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Security id={self.id} symbol={self.symbol!r} {self.currency}>"


# ---------------------------------------------------------------------------
# investment_transactions  (the ledger; positions are derived from it)
# ---------------------------------------------------------------------------
class InvestmentTransaction(Base):
    """One broker operation of one user.

    Money fields by kind (EUR amounts are positive and rounded to cents):
      buy      amount_eur = total EUR charged, fees included
      sell     amount_eur = net EUR received, fees already deducted
      dividend amount_eur = net EUR received; gross = amount + tax_withheld_eur
      grant    amount_eur = tax cost basis (market value when received);
               cash_paid_eur = EUR actually paid (0 for free shares)
      split    split_ratio = new shares per old share (2.0 for a 2-for-1)
    `price` is per share in `currency` (the security's quote currency);
    with amount_eur it gives the FX rate the broker applied.
    """

    __tablename__ = "investment_transactions"
    __table_args__ = (
        Index("ix_investment_transactions_user_date", "user_id", "trade_date"),
        Index("ix_investment_transactions_user_security", "user_id", "security_id"),
        # An imported operation is recognised by its broker reference, so the
        # same statement can be imported twice without duplicates (v5).
        Index("ux_investment_transactions_broker_ref", "user_id", "broker", "broker_ref",
              unique=True, sqlite_where=text("broker_ref IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    security_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False
    )
    broker: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[InvestmentKind] = mapped_column(Enum(InvestmentKind), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)

    quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    amount_eur: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fees_eur: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tax_withheld_eur: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cash_paid_eur: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    split_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Broker's own id (DEGIRO order id, or a stable key for a dividend) when
    # the operation came from an imported statement; None when typed by hand.
    broker_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, onupdate=func.now(), nullable=True
    )

    user: Mapped[User] = relationship("User", back_populates="investment_transactions")
    security: Mapped[Security] = relationship("Security", back_populates="transactions")

    def __repr__(self) -> str:
        return (
            f"<InvestmentTransaction id={self.id} {self.kind} {self.trade_date} "
            f"security_id={self.security_id} qty={self.quantity} eur={self.amount_eur}>"
        )


# ---------------------------------------------------------------------------
# price_history / fx_rates  (local cache of market data)
# ---------------------------------------------------------------------------
class PriceHistory(Base):
    """Daily close of a security in its own quote currency."""

    __tablename__ = "price_history"

    security_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    # PRICE_SOURCE_YAHOO | PRICE_SOURCE_MANUAL
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PRICE_SOURCE_YAHOO
    )

    security: Mapped[Security] = relationship("Security", back_populates="prices")

    def __repr__(self) -> str:
        return f"<PriceHistory security_id={self.security_id} {self.price_date} {self.close}>"


class FxRate(Base):
    """Units of `currency` per 1 EUR on a day (Yahoo's EURUSD=X convention)."""

    __tablename__ = "fx_rates"

    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rate: Mapped[float] = mapped_column(Float, nullable=False)

    def __repr__(self) -> str:
        return f"<FxRate {self.currency} {self.rate_date} {self.rate}>"


# ---------------------------------------------------------------------------
# broker_cash  (uninvested cash at each broker, v6)
# ---------------------------------------------------------------------------
class BrokerCash(Base):
    """Cash a user holds at a broker in one currency, as of a day.

    DEGIRO's «Margen libre»: taken from the newest «Saldo» of an imported
    account statement (an older statement never overwrites a newer value).
    Together with the positions it gives the whole account value.
    """

    __tablename__ = "broker_cash"
    __table_args__ = (
        UniqueConstraint("user_id", "broker", "currency", name="ux_broker_cash_user_broker_currency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    broker: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="extracto")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="broker_cash")

    def __repr__(self) -> str:
        return f"<BrokerCash user={self.user_id} {self.broker} {self.amount} {self.currency} {self.as_of}>"