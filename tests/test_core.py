from decimal import Decimal

import pytest

from app.core import budgets, ledger
from app.core.ledger import InsufficientFunds
from app.core.types import AccountType, parse_account
from app.db.session import session_scope

CASH, BANK = AccountType.CASH, AccountType.BANK


def test_parse_account():
    assert parse_account("paid by UPI") == BANK
    assert parse_account("in hand") == CASH
    assert parse_account("cash from bank") is None
    assert parse_account("ice cream") is None


async def test_expense_income_transfer_and_undo(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 5000, "salary", "salary", BANK)
        await ledger.record_transfer(s, user_id, 300, BANK, CASH)
        await ledger.record_expense(s, user_id, 120, "food", "ice cream", CASH)
        bal = await ledger.get_balances(s, user_id)
    assert (bal["bank"], bal["cash"]) == (Decimal("4700"), Decimal("180"))

    async with session_scope() as s:
        await ledger.void_transaction(s, user_id)  # undo the ice cream
        bal = await ledger.get_balances(s, user_id)
    assert bal["cash"] == Decimal("300")


async def test_overdraft_is_rejected_and_rolled_back(user_id):
    with pytest.raises(InsufficientFunds):
        async with session_scope() as s:
            await ledger.record_income(s, user_id, 100, "x", "x", CASH)
            await ledger.record_expense(s, user_id, 500, "food", "feast", CASH)
    async with session_scope() as s:
        assert (await ledger.get_balances(s, user_id))["cash"] == 0  # income rolled back too


async def test_budget_alerts_fire_once_per_threshold(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 10000, "salary", "s", BANK)
        await budgets.set_budget(s, user_id, "Food", 1000)

    async def spend(amount):
        async with session_scope() as s:
            await ledger.record_expense(s, user_id, amount, "food", "x", BANK)
            return await budgets.evaluate_alerts(s, user_id, "food")

    assert await spend(300) is None                      # 30%
    assert "50%" in await spend(250)                     # 55%
    assert await spend(50) is None                       # 60% - 50% already sent
    assert "80%" in await spend(250)                     # 85%
    assert "EXCEEDED" in await spend(400)                # 125%
    assert await spend(10) is None                       # nothing new
