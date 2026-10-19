"""Read-only queries: history and summaries."""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.periods import Period, month_key, month_period, shift_month
from app.core.types import TxnKind
from app.db.models import Transaction


def _filters(user_id: int, period: Period, include_voided: bool = False) -> list:
    conds = [Transaction.user_id == user_id]
    if not include_voided:
        conds.append(Transaction.voided_at.is_(None))
    if period.start:
        conds.append(Transaction.occurred_at >= period.start)
    if period.end:
        conds.append(Transaction.occurred_at < period.end)
    return conds


async def list_transactions(
    session: AsyncSession,
    user_id: int,
    *,
    period: Period,
    limit: int = 10,
    kind: str | None = None,
    category: str | None = None,
    account: str | None = None,
) -> list[Transaction]:
    q = select(Transaction).where(*_filters(user_id, period))
    if kind:
        q = q.where(Transaction.kind == kind)
    if category:
        q = q.where(Transaction.category == category.strip().lower())
    if account:
        q = q.where((Transaction.from_account == account) | (Transaction.to_account == account))
    q = q.order_by(Transaction.occurred_at.desc(), Transaction.id.desc()).limit(max(1, min(limit, 50)))
    return list((await session.execute(q)).scalars().all())


async def summary(session: AsyncSession, user_id: int, period: Period) -> dict:
    conds = _filters(user_id, period)
    by_cat = (
        await session.execute(
            select(Transaction.category, func.sum(Transaction.amount))
            .where(*conds, Transaction.kind == TxnKind.EXPENSE.value)
            .group_by(Transaction.category)
            .order_by(func.sum(Transaction.amount).desc())
        )
    ).all()
    income = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(*conds, Transaction.kind == TxnKind.INCOME.value)
        )
    ).scalar_one()
    return {
        "expenses_by_category": [(c or "other", Decimal(t)) for c, t in by_cat],
        "total_spent": sum((Decimal(t) for _, t in by_cat), Decimal(0)),
        "total_income": Decimal(income),
    }


def _local(col):
    """A timestamptz column as local wall-clock time, so months/days follow the user's timezone."""
    return func.timezone(settings.timezone, col)


async def monthly_trend(session: AsyncSession, user_id: int, *, months: int = 6, category: str | None = None) -> dict:
    """Spent / income per calendar month for the last `months` months (including the current one)."""
    months = max(1, min(months, 24))
    keys = [shift_month(month_key(), -i) for i in range(months - 1, -1, -1)]
    m = func.date_trunc("month", _local(Transaction.occurred_at))
    q = (
        select(m, Transaction.kind, Transaction.category, func.sum(Transaction.amount))
        .where(
            Transaction.user_id == user_id,
            Transaction.voided_at.is_(None),
            Transaction.kind.in_([TxnKind.EXPENSE.value, TxnKind.INCOME.value]),
            Transaction.occurred_at >= month_period(keys[0]).start,
            Transaction.occurred_at < month_period(keys[-1]).end,
        )
        .group_by(m, Transaction.kind, Transaction.category)
    )
    if category:
        q = q.where(Transaction.category == category.strip().lower())
    spent = {k: Decimal(0) for k in keys}
    income = {k: Decimal(0) for k in keys}
    by_cat: dict[str, dict[str, Decimal]] = {}
    for month, kind, cat, total in (await session.execute(q)).all():
        k = month.strftime("%Y-%m")
        if k not in spent:
            continue
        if kind == TxnKind.EXPENSE.value:
            spent[k] += Decimal(total)
            by_cat.setdefault(cat or "other", {k2: Decimal(0) for k2 in keys})[k] += Decimal(total)
        else:
            income[k] += Decimal(total)
    return {"months": keys, "spent": spent, "income": income, "by_category": by_cat}


async def daily_breakdown(session: AsyncSession, user_id: int, month: str) -> dict:
    """Spending per day inside one month, plus that month's income."""
    p = month_period(month)
    d = func.date(_local(Transaction.occurred_at))
    base = [Transaction.user_id == user_id, Transaction.voided_at.is_(None),
            Transaction.occurred_at >= p.start, Transaction.occurred_at < p.end]
    rows = (
        await session.execute(
            select(d, func.sum(Transaction.amount), func.count(), )
            .where(*base, Transaction.kind == TxnKind.EXPENSE.value)
            .group_by(d).order_by(d)
        )
    ).all()
    top = (
        await session.execute(
            select(d, Transaction.category, func.sum(Transaction.amount))
            .where(*base, Transaction.kind == TxnKind.EXPENSE.value)
            .group_by(d, Transaction.category)
            .order_by(d, func.sum(Transaction.amount).desc())
        )
    ).all()
    top_cat: dict[date, str] = {}
    for day, cat, _ in top:
        top_cat.setdefault(day, cat or "other")  # first row per day = its biggest category
    income = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(*base, Transaction.kind == TxnKind.INCOME.value)
        )
    ).scalar_one()
    return {
        "label": p.label,
        "days": [(day, Decimal(total), n, top_cat.get(day, "other")) for day, total, n in rows],
        "income": Decimal(income),
    }
