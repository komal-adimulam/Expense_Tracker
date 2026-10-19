"""Ready-to-send text for read-only requests. Used by the LLM tools AND by the bot's no-LLM buttons."""
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import budgets, ledger, reports
from app.core.formatting import balance_card, bar, cat_icon, history_line, money, status_dot
from app.core.periods import PeriodError, month_key, month_label, month_period, resolve_period


async def balance_text(s: AsyncSession, user_id: int) -> str:
    return balance_card(await ledger.get_balances(s, user_id))


async def budgets_text(s: AsyncSession, user_id: int, month: str | None = None) -> str:
    """Budget progress for the current month, or for a past month ('YYYY-MM')."""
    period = month_period(month) if month else None
    rows = await budgets.list_budget_status(s, user_id, period)
    if not rows:
        return "🎯 No budgets yet.\n\nTry saying: set a limit of 3k for food"
    lines = [f"🎯 Budgets · {period.label if period else 'this month'}\n"]
    for r in rows:
        left = f"{money(r.remaining)} left" if r.remaining >= 0 else f"over by {money(-r.remaining)}"
        lines.append(
            f"{status_dot(r.percent)} {cat_icon(r.category)} {r.category}\n"
            f"   {bar(r.percent)} {r.percent}%\n"
            f"   {money(r.spent)} of {money(r.limit)} · {left}"
        )
    return "\n".join(lines)


async def recent_text(
    s: AsyncSession, user_id: int, *, period: str = "all_time", limit: int = 10,
    kind: str | None = None, category: str | None = None, account: str | None = None,
) -> str:
    rows = await reports.list_transactions(
        s, user_id, period=resolve_period(period), limit=limit, kind=kind, category=category, account=account
    )
    if not rows:
        return "🧾 No transactions found."
    return "🧾 Recent transactions\n\n" + "\n".join(history_line(t) for t in rows)


async def summary_text(s: AsyncSession, user_id: int, period: str = "this_month") -> str:
    p = resolve_period(period)
    data = await reports.summary(s, user_id, p)
    lines = [f"📊 Summary · {p.label}\n", f"🟢 Income  {money(data['total_income'])}", f"🔴 Spent   {money(data['total_spent'])}"]
    total = data["total_spent"] or 1
    if data["expenses_by_category"]:
        lines.append("")
    for cat, amount in data["expenses_by_category"]:
        pct = int(amount * 100 / total)
        lines.append(f"{cat_icon(cat)} {cat:<12} {money(amount)}  ({pct}%)")
    return "\n".join(lines)


async def trend_text(s: AsyncSession, user_id: int, months: int = 6, category: str | None = None) -> str:
    data = await reports.monthly_trend(s, user_id, months=months, category=category)
    keys, spent, income = data["months"], data["spent"], data["income"]
    title = f"📈 {category.strip().lower()} · last {len(keys)} months" if category else f"📈 Last {len(keys)} months"
    if not any(spent.values()) and not any(income.values()):
        return f"{title}\n\nNo transactions in this range yet."
    top = max(spent.values()) or Decimal(1)
    lines = [title, ""]
    for k in keys:
        pct = int(spent[k] * 100 / top)
        inc = f"  🟢 {money(income[k])}" if income[k] and not category else ""
        lines.append(f"{month_label(k)}  {bar(pct, 8)} 🔴 {money(spent[k])}{inc}")
    total = sum(spent.values(), Decimal(0))
    lines += ["", f"Total spent {money(total)} · avg {money(total / len(keys))}/month"]

    if not category and data["by_category"]:
        lines += ["", "By category"]
        ranked = sorted(data["by_category"].items(), key=lambda kv: -sum(kv[1].values()))[:5]
        for cat, per_month in ranked:
            cells = " · ".join(f"{month_label(k)[:3]} {money(per_month[k])}" for k in keys[-4:])
            lines.append(f"{cat_icon(cat)} {cat}: {cells}")
    return "\n".join(lines)


async def daily_text(s: AsyncSession, user_id: int, month: str | None = None) -> str:
    month = month or month_key()
    data = await reports.daily_breakdown(s, user_id, month)
    if not data["days"]:
        return f"📅 {data['label']}\n\nNo spending recorded."
    peak = max(t for _, t, _, _ in data["days"]) or Decimal(1)
    total = sum((t for _, t, _, _ in data["days"]), Decimal(0))
    lines = [f"📅 Daily spending · {data['label']}", ""]
    for day, amount, n, cat in data["days"]:
        lines.append(f"{day:%d %a}  {bar(int(amount * 100 / peak), 8)} {money(amount)}  {cat_icon(cat)} {cat}" + (f" +{n - 1}" if n > 1 else ""))
    best = max(data["days"], key=lambda r: r[1])
    lines += ["", f"🔴 Total {money(total)} over {len(data['days'])} days · avg {money(total / len(data['days']))}/day",
              f"🔝 Biggest day: {best[0]:%d %b} ({money(best[1])})"]
    if data["income"]:
        lines.append(f"🟢 Income this month: {money(data['income'])}")
    return "\n".join(lines)
