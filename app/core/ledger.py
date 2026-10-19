"""The ledger: the only place that changes balances."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.formatting import money
from app.core.types import AccountType, TxnKind
from app.db.models import Account, Transaction, User

CENT = Decimal("0.01")


class LedgerError(Exception):
    """A user-facing, expected failure (bad amount, not enough money, ...)."""


class InsufficientFunds(LedgerError):
    pass


def to_decimal(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def normalize_category(category: str | None) -> str:
    return (category or "other").strip().lower()[:64] or "other"


# ---------------------------------------------------------------- users

async def get_or_create_user(session: AsyncSession, telegram_id: int, name: str | None = None) -> User:
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if user:
        return user
    await session.execute(
        pg_insert(User).values(telegram_id=telegram_id, name=name).on_conflict_do_nothing(index_elements=["telegram_id"])
    )
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
    for acc in AccountType:
        await session.execute(
            pg_insert(Account)
            .values(user_id=user.id, type=acc.value, balance=Decimal("0"))
            .on_conflict_do_nothing(constraint="uq_account_user_type")
        )
    return user


async def get_balances(session: AsyncSession, user_id: int) -> dict[str, Decimal]:
    rows = (await session.execute(select(Account).where(Account.user_id == user_id))).scalars().all()
    out = {a.value: Decimal("0") for a in AccountType}
    out.update({r.type: r.balance for r in rows})
    return out


# ---------------------------------------------------------------- posting

async def _lock_accounts(session: AsyncSession, user_id: int, types: set[AccountType]) -> dict[str, Account]:
    """Row-lock accounts in a fixed order so concurrent requests cannot deadlock."""
    rows = (
        await session.execute(
            select(Account)
            .where(Account.user_id == user_id, Account.type.in_([t.value for t in types]))
            .order_by(Account.type)
            .with_for_update()
        )
    ).scalars().all()
    return {a.type: a for a in rows}


async def post(
    session: AsyncSession,
    *,
    user_id: int,
    kind: TxnKind,
    amount: Decimal | float,
    from_account: AccountType | None = None,
    to_account: AccountType | None = None,
    category: str | None = None,
    description: str | None = None,
    occurred_at: datetime | None = None,
    external_key: str | None = None,
    import_id: int | None = None,
    allow_negative: bool = False,
) -> Transaction:
    amount = to_decimal(amount)
    if amount <= 0:
        raise LedgerError("Amount must be greater than zero.")
    if from_account and from_account == to_account:
        raise LedgerError("Source and destination account are the same.")

    accounts = await _lock_accounts(session, user_id, {a for a in (from_account, to_account) if a})
    if from_account:
        src = accounts[from_account.value]
        if not (allow_negative or settings.allow_negative_balance) and src.balance < amount:
            raise InsufficientFunds(
                f"Not enough in {from_account.value}: balance is {money(src.balance)}, needed {money(amount)}."
            )
        src.balance -= amount
    if to_account:
        accounts[to_account.value].balance += amount

    txn = Transaction(
        user_id=user_id,
        kind=kind.value,
        amount=amount,
        category=normalize_category(category) if category else None,
        description=(description or "").strip()[:256] or None,
        from_account=from_account.value if from_account else None,
        to_account=to_account.value if to_account else None,
        external_key=external_key,
        import_id=import_id,
    )
    if occurred_at:
        txn.occurred_at = occurred_at
    session.add(txn)
    await session.flush()
    return txn


async def record_expense(session, user_id, amount, category, description, account: AccountType, occurred_at=None):
    return await post(
        session, user_id=user_id, kind=TxnKind.EXPENSE, amount=amount, from_account=account,
        category=category, description=description, occurred_at=occurred_at,
    )


async def record_income(session, user_id, amount, category, description, account: AccountType, occurred_at=None):
    return await post(
        session, user_id=user_id, kind=TxnKind.INCOME, amount=amount, to_account=account,
        category=category, description=description, occurred_at=occurred_at,
    )


async def record_transfer(session, user_id, amount, from_account: AccountType, to_account: AccountType, note=None):
    return await post(
        session, user_id=user_id, kind=TxnKind.TRANSFER, amount=amount, from_account=from_account,
        to_account=to_account, category="transfer", description=note,
    )


async def set_balance(session: AsyncSession, user_id: int, account: AccountType, new_balance: Decimal | float) -> Transaction | None:
    """Manual correction / opening balance. Recorded as an adjustment so history stays auditable."""
    new_balance = to_decimal(new_balance)
    acc = (await _lock_accounts(session, user_id, {account}))[account.value]
    diff = new_balance - acc.balance
    if diff == 0:
        return None
    if diff > 0:
        acc.balance += diff
        txn = Transaction(user_id=user_id, kind=TxnKind.ADJUSTMENT.value, amount=diff,
                          to_account=account.value, description="balance correction")
    else:
        acc.balance += diff
        txn = Transaction(user_id=user_id, kind=TxnKind.ADJUSTMENT.value, amount=-diff,
                          from_account=account.value, description="balance correction")
    session.add(txn)
    await session.flush()
    return txn


async def void_transaction(session: AsyncSession, user_id: int, txn_id: int | None = None) -> Transaction:
    """Undo a transaction (default: the most recent active one) by reversing its effect."""
    q = select(Transaction).where(Transaction.user_id == user_id, Transaction.voided_at.is_(None))
    q = q.where(Transaction.id == txn_id) if txn_id else q.order_by(Transaction.id.desc()).limit(1)
    txn = (await session.execute(q.with_for_update())).scalar_one_or_none()
    if txn is None:
        raise LedgerError("No matching transaction to undo.")

    sides = {AccountType(a) for a in (txn.from_account, txn.to_account) if a}
    accounts = await _lock_accounts(session, user_id, sides)
    if txn.from_account:
        accounts[txn.from_account].balance += txn.amount
    if txn.to_account:
        accounts[txn.to_account].balance -= txn.amount
    txn.voided_at = datetime.now(timezone.utc)
    await session.flush()
    return txn
