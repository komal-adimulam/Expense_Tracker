from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core import budgets, ledger
from app.core.formatting import balances_line, friendly_txn, money
from app.core.ledger import LedgerError
from app.core.periods import parse_when
from app.core.types import AccountType, parse_account
from app.db.session import session_scope
from app.tools._common import ask_account, user_id_from

_ACCOUNT_HELP = (
    "'cash' or 'bank'. Set ONLY if the user explicitly said so (UPI/card/GPay/online/netbanking = bank). "
    "Otherwise leave null - the system will ask the user. NEVER guess."
)


class ExpenseItem(BaseModel):
    amount: float = Field(gt=0, description="Amount spent, as a plain number (3k = 3000).")
    category: str = Field(description="One lowercase word, e.g. food, transport, clothes, bills, shopping. Prefer the user's budget categories.")
    description: str = Field(description="What it was for, e.g. 'ice cream'.")
    account: str | None = Field(default=None, description=_ACCOUNT_HELP)
    date: str | None = Field(default=None, description="YYYY-MM-DD, only if the user refers to a past day (e.g. 'yesterday').")


class IncomeItem(BaseModel):
    amount: float = Field(gt=0, description="Amount received, as a plain number.")
    category: str = Field(default="income", description="e.g. salary, refund, gift, freelance, income.")
    description: str = Field(description="Source, e.g. 'October salary'.")
    account: str | None = Field(default=None, description=_ACCOUNT_HELP)
    date: str | None = Field(default=None, description="YYYY-MM-DD, only for a past day.")


def _resolve_missing(items: list, verb: str) -> tuple[AccountType | None, bool]:
    """Ask once for all items lacking an account. Returns (choice, cancelled)."""
    missing = [i for i in items if parse_account(i.account) is None]
    if not missing:
        return None, False
    what = ", ".join(f"{money(i.amount)} {i.description}" for i in missing)
    choice = ask_account(f"Was this {verb} cash or bank? ({what})")
    return choice, choice is None


@tool
async def add_expenses(items: list[ExpenseItem], config: RunnableConfig) -> str:
    """Record one or more expenses (money spent / paid / bought). Debits cash or bank.
    Put ALL expenses mentioned in the user's message into one call."""
    items = [ExpenseItem.model_validate(i) if isinstance(i, dict) else i for i in items]
    fallback, cancelled = _resolve_missing(items, "paid from")
    if cancelled:
        return "👍 Cancelled - nothing was recorded."

    uid = user_id_from(config)
    try:
        async with session_scope() as s:
            lines, categories = [], []
            for i in items:
                acct = parse_account(i.account) or fallback
                t = await ledger.record_expense(s, uid, i.amount, i.category, i.description, acct, parse_when(i.date))
                lines.append(friendly_txn(t))
                categories.append(t.category)
            alerts = [a for c in dict.fromkeys(categories) if (a := await budgets.evaluate_alerts(s, uid, c))]
            bal = balances_line(await ledger.get_balances(s, uid))
    except LedgerError as e:
        return f"❌ FAILED, nothing recorded: {e}"
    return "\n".join([*lines, *alerts, bal])


@tool
async def add_income(items: list[IncomeItem], config: RunnableConfig) -> str:
    """Record money received (salary, refund, gift, cash received). Credits cash or bank."""
    items = [IncomeItem.model_validate(i) if isinstance(i, dict) else i for i in items]
    fallback, cancelled = _resolve_missing(items, "received in")
    if cancelled:
        return "👍 Cancelled - nothing was recorded."

    uid = user_id_from(config)
    try:
        async with session_scope() as s:
            lines = []
            for i in items:
                acct = parse_account(i.account) or fallback
                t = await ledger.record_income(s, uid, i.amount, i.category, i.description, acct, parse_when(i.date))
                lines.append(friendly_txn(t))
            bal = balances_line(await ledger.get_balances(s, uid))
    except LedgerError as e:
        return f"❌ FAILED, nothing recorded: {e}"
    return "\n".join([*lines, bal])


@tool
async def set_account_balance(account: str, balance: float, config: RunnableConfig) -> str:
    """Set an account's balance to an exact figure (opening balance or correction).
    account must be 'cash' or 'bank'. Only for explicit statements like 'my bank balance is 5000'."""
    acct = parse_account(account)
    if acct is None:
        return "❌ FAILED: account must be 'cash' or 'bank'."
    async with session_scope() as s:
        t = await ledger.set_balance(s, user_id_from(config), acct, balance)
        bal = balances_line(await ledger.get_balances(s, user_id_from(config)))
    return (friendly_txn(t) if t else f"{acct.value} already had that balance.") + f"\n{bal}"


@tool
async def undo_transaction(config: RunnableConfig, transaction_id: int | None = None) -> str:
    """Undo/delete a transaction and reverse its effect on the balances.
    Omit transaction_id to undo the most recent one."""
    try:
        async with session_scope() as s:
            t = await ledger.void_transaction(s, user_id_from(config), transaction_id)
            bal = balances_line(await ledger.get_balances(s, user_id_from(config)))
    except LedgerError as e:
        return f"❌ FAILED: {e}"
    return f"↩️ Undone: {friendly_txn(t)}\n{bal}"
