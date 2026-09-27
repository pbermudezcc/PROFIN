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


# Transaction.category_source values. Stored as plain strings (not an Enum
# column) so the column can be added to existing SQLite files with ALTER TABLE.
CATEGORY_SOURCE_MANUAL = "manual"   # set by a family member — rules never touch it
CATEGORY_SOURCE_RULE   = "rule"     # set by the auto-categorization rules engine


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
        "Category", back_populates="user", cascade="all, delete-orphan"
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="categories")
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