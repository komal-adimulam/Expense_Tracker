from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core import budgets, ledger, views
from app.core.periods import (
    PeriodError, month_key, month_label, month_period, now_local, resolve_period, shift_month, tz,
)
from app.core.types import AccountType
from app.db.session import session_scope

BANK = AccountType.BANK
CUR = month_key()
PREV = shift_month(CUR, -1)
PREV2 = shift_month(CUR, -2)


def at(month: str, day: int, hour=12, minute=0) -> datetime:
    return datetime(int(month[:4]), int(month[5:7]), day, hour, minute, tzinfo=tz())


async def spend(uid, amount, category, when, desc="x"):
    async with session_scope() as s:
        await ledger.record_expense(s, uid, amount, category, desc, BANK, when)


@pytest.fixture
async def rich_user(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 1_000_000, "salary", "seed", BANK)
    await spend(user_id, 100, "food", at(PREV2, 5))
    await spend(user_id, 200, "food", at(PREV, 3), "lunch")
    await spend(user_id, 50, "transport", at(PREV, 3), "bus")      # same day as lunch
    await spend(user_id, 400, "food", at(PREV, 20))
    await spend(user_id, 70, "food", at(CUR, 1, 0, 30))             # 00:30 on the 1st -> this month
    return user_id


# ------------------------------------------------------------------ period parsing
def test_shift_month_wraps_years():
    assert shift_month("2026-01", -1) == "2025-12"
    assert shift_month("2025-12", 1) == "2026-01"
    assert shift_month("2026-03", -15) == "2024-12"
    assert month_label("2026-09") == "Sep 2026"


def test_resolve_specific_month_day_and_range():
    p = resolve_period("2026-09")
    assert p.label == "Sep 2026"
    assert (p.end - p.start) == timedelta(days=30)

    d = resolve_period("2026-09-15")
    assert (d.end - d.start) == timedelta(days=1)

    r = resolve_period("2026-10-01..2026-10-15")  # inclusive of the 15th
    assert (r.end - r.start) == timedelta(days=15)
    assert resolve_period("2026-10-15..2026-10-01").start == r.start  # reversed range is fixed up


def test_february_and_leap_year_lengths():
    assert (month_period("2024-02").end - month_period("2024-02").start) == timedelta(days=29)
    assert (month_period("2026-02").end - month_period("2026-02").start) == timedelta(days=28)


@pytest.mark.parametrize("bad", ["2026-13", "2026-02-30", "september", "next_week", "2026-10-01..2026-13-01"])
def test_bad_periods_raise_a_clear_error(bad):
    with pytest.raises(PeriodError):
        resolve_period(bad)


def test_named_periods_still_work():
    for name in ("today", "yesterday", "this_week", "last_7_days", "last_30_days", "this_month", "last_month", "this_year", "all_time"):
        resolve_period(name)
    assert resolve_period("all_time").start is None


# ------------------------------------------------------------------ reports
async def test_summary_for_a_specific_month(rich_user):
    async with session_scope() as s:
        text = await views.summary_text(s, rich_user, PREV)
    assert "₹650" in text and "food" in text and "transport" in text  # 200+50+400, not the other months
    assert "₹100" not in text.split("Spent")[1].split("\n")[0]


async def test_summary_for_a_date_range(rich_user):
    async with session_scope() as s:
        text = await views.summary_text(s, rich_user, f"{PREV}-01..{PREV}-10")
    assert "₹250" in text  # lunch 200 + bus 50, not the 20th


async def test_monthly_trend_totals_and_category_view(rich_user):
    async with session_scope() as s:
        text = await views.trend_text(s, rich_user, months=3)
        food = await views.trend_text(s, rich_user, months=3, category="food")
    assert "₹100" in text and "₹650" in text and "₹70" in text
    assert "Total spent ₹820" in text
    assert "food:" in text and "transport:" in text
    assert "food · last 3 months" in food and "Total spent ₹770" in food  # transport excluded


async def test_trend_with_no_data(user_id):
    async with session_scope() as s:
        assert "No transactions" in await views.trend_text(s, user_id)


async def test_daily_breakdown(rich_user):
    async with session_scope() as s:
        text = await views.daily_text(s, rich_user, PREV)
    assert "03 " in text and "₹250" in text and "+1" in text      # two items on the 3rd, biggest category shown
    assert "20 " in text and "₹400" in text
    assert "Biggest day" in text and "over 2 days" in text


async def test_month_boundary_follows_local_timezone(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 10_000, "x", "seed", BANK)
    last_day = (date(int(CUR[:4]), int(CUR[5:7]), 1) - timedelta(days=1)).day  # last day of the previous month
    await spend(user_id, 11, "food", at(PREV, last_day, 23, 30))     # 23:30 on the last day -> previous month
    await spend(user_id, 22, "food", at(CUR, 1, 0, 30))              # 00:30 on the 1st -> current month
    async with session_scope() as s:
        prev = await views.summary_text(s, user_id, PREV)
        cur = await views.summary_text(s, user_id, CUR)
    assert "₹11" in prev and "₹22" not in prev
    assert "₹22" in cur and "₹11" not in cur.split("Spent")[1].split("\n")[0]


async def test_budget_view_for_a_past_month(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 10_000, "x", "seed", BANK)
        await budgets.set_budget(s, user_id, "food", 1000)
    await spend(user_id, 500, "food", at(PREV, 10))
    async with session_scope() as s:
        past = await views.budgets_text(s, user_id, PREV)
        now = await views.budgets_text(s, user_id)
    assert month_label(PREV) in past and "₹500 of ₹1,000" in past and "50%" in past
    assert "₹0 of ₹1,000" in now
