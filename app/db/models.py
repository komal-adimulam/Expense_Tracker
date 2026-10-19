from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(14, 2)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    """One row per (user, cash|bank). `balance` is kept in sync with the transactions table."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("user_id", "type", name="uq_account_user_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(8))  # AccountType value
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))


class Transaction(Base):
    """
    Double-entry-lite: money leaves `from_account` and/or enters `to_account`.
      expense    from=X  to=None
      income     from=None to=X
      transfer   from=X  to=Y
      adjustment one side only (manual balance correction)
    Rows are never deleted; undo sets `voided_at` and reverses the balance effect.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_txn_amount_positive"),
        Index("ix_txn_user_time", "user_id", "occurred_at"),
        Index("ix_txn_user_category", "user_id", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12))  # TxnKind value
    amount: Mapped[Decimal] = mapped_column(MONEY)
    category: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(256))
    from_account: Mapped[str | None] = mapped_column(String(8))
    to_account: Mapped[str | None] = mapped_column(String(8))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # set for rows created by a statement import: dedupe key + which import produced it
    external_key: Mapped[str | None] = mapped_column(String(40), index=True)
    import_id: Mapped[int | None] = mapped_column(ForeignKey("statement_imports.id", ondelete="SET NULL"), index=True)


class StatementImport(Base):
    """A parsed bank-statement PDF waiting for the user's confirmation (then 'done', or 'undone')."""

    __tablename__ = "statement_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|done|cancelled|undone
    items: Mapped[list] = mapped_column(JSONB)
    meta: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_budget_user_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(64))
    monthly_limit: Mapped[Decimal] = mapped_column(MONEY)


class BudgetAlert(Base):
    """Remembers which thresholds were already announced this month so alerts fire once."""

    __tablename__ = "budget_alerts"
    __table_args__ = (UniqueConstraint("budget_id", "period", "threshold", name="uq_alert_once"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7))  # "2026-10"
    threshold: Mapped[int]
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
