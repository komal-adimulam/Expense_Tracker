from decimal import Decimal

from langchain_core.messages import AIMessage

from app.core import ledger
from app.core.types import AccountType
from app.db.session import session_scope
from tests.conftest import tool_call

ICE = {"items": [{"amount": 300, "category": "food", "description": "ice cream"}]}


async def _balances(uid):
    async with session_scope() as s:
        return await ledger.get_balances(s, uid)


async def _fund(uid, cash=1000, bank=0):
    async with session_scope() as s:
        if cash:
            await ledger.record_income(s, uid, cash, "x", "seed", AccountType.CASH)
        if bank:
            await ledger.record_income(s, uid, bank, "x", "seed", AccountType.BANK)


async def test_missing_account_asks_human_then_debits(user_id, make_runner):
    await _fund(user_id)
    runner = make_runner(tool_call("add_expenses", ICE))

    ask = await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="spent 300 on ice cream")
    assert ask.choices == ["cash", "bank", "cancel"]
    assert (await _balances(user_id))["cash"] == 1000  # nothing written while waiting

    done = await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="cash")
    assert "ice cream" in done.text and "₹700" in done.text  # reply is built from the tool output
    assert len(done.txn_ids) == 1                              # -> bot shows an Undo button
    assert (await _balances(user_id))["cash"] == Decimal("700")


async def test_account_given_skips_question(user_id, make_runner):
    await _fund(user_id, cash=0, bank=1000)
    args = {"items": [{**ICE["items"][0], "account": "upi"}]}
    runner = make_runner(tool_call("add_expenses", args))
    reply = await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="300 ice cream by upi")
    assert reply.choices == []
    assert (await _balances(user_id))["bank"] == Decimal("700")


async def test_withdraw_moves_bank_to_cash(user_id, make_runner):
    await _fund(user_id, cash=0, bank=1000)
    runner = make_runner(
        tool_call("transfer_funds", {"amount": 300, "from_account": "bank", "to_account": "cash"}),
    )
    await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="withdrew 300")
    bal = await _balances(user_id)
    assert (bal["bank"], bal["cash"]) == (Decimal("700"), Decimal("300"))


async def test_unrelated_reply_cancels_pending_question(user_id, make_runner):
    await _fund(user_id)
    runner = make_runner(
        tool_call("add_expenses", ICE),            # 1st request -> asks
        tool_call("get_balances", {}, "call_2"),   # new request (cancel needs no LLM call)
    )
    await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="spent 300 on ice cream")
    reply = await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="what's my balance?")
    assert "Cash" in reply.text and "₹1,000" in reply.text
    assert (await _balances(user_id))["cash"] == 1000  # the expense was dropped


async def test_two_tool_calls_in_one_turn_run_sequentially(user_id, make_runner):
    await _fund(user_id, cash=0, bank=0)
    both = AIMessage("", tool_calls=[
        {"name": "add_income", "args": {"items": [{"amount": 500, "description": "salary"}]}, "id": "a", "type": "tool_call"},
        {"name": "add_expenses", "args": ICE, "id": "b", "type": "tool_call"},
    ])
    runner = make_runner(both)
    tid = f"t{user_id}"
    q1 = await runner.chat(user_id=user_id, thread_id=tid, text="got 500 salary and spent 300")
    assert "received in" in q1.text
    q2 = await runner.chat(user_id=user_id, thread_id=tid, text="bank")
    assert "paid from" in q2.text
    # income was written exactly once even though the graph resumed twice
    assert (await _balances(user_id))["bank"] == Decimal("500")
    await runner.chat(user_id=user_id, thread_id=tid, text="bank")
    assert (await _balances(user_id))["bank"] == Decimal("200")


async def test_tool_error_goes_back_to_llm_for_a_retry(user_id, make_runner):
    await _fund(user_id, cash=0, bank=1000)
    bad = tool_call("add_expenses", {"items": [{"amount": -5, "category": "food", "description": "x"}]})
    good = tool_call("add_expenses", {"items": [{**ICE["items"][0], "account": "bank"}]}, "call_2")
    runner = make_runner(bad, good)
    reply = await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="spent 300 on ice cream by card")
    assert (await _balances(user_id))["bank"] == Decimal("700")
    assert reply.txn_ids


async def test_plain_chat_needs_no_tool(user_id, make_runner):
    runner = make_runner(tool_call("respond_to_user", {"kind": "greeting"}))
    reply = await runner.chat(user_id=user_id, thread_id=f"t{user_id}", text="hello")
    assert reply.text.startswith("👋") and not reply.txn_ids
