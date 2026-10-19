from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.core import ledger
from app.core.formatting import balances_line, friendly_txn
from app.core.ledger import LedgerError
from app.core.types import parse_account
from app.db.session import session_scope
from app.tools._common import user_id_from


@tool
async def transfer_funds(amount: float, from_account: str, to_account: str, config: RunnableConfig, note: str | None = None) -> str:
    """Move money between the user's own accounts. Not an expense or income.
    'withdrew 300 from ATM / took 300 out of bank' -> from_account='bank', to_account='cash'.
    'deposited 500 cash in bank' -> from_account='cash', to_account='bank'.
    Accounts must be 'cash' or 'bank'."""
    src, dst = parse_account(from_account), parse_account(to_account)
    if src is None or dst is None:
        return "❌ FAILED: both accounts must be 'cash' or 'bank'."
    try:
        async with session_scope() as s:
            uid = user_id_from(config)
            t = await ledger.record_transfer(s, uid, amount, src, dst, note)
            bal = balances_line(await ledger.get_balances(s, uid))
    except LedgerError as e:
        return f"❌ FAILED, nothing moved: {e}"
    return f"{friendly_txn(t)}\n{bal}"
