"""Every tool the agent can call. Add a new tool: write it in a module, list it here."""
from app.tools.conversation import respond_to_user
from app.tools.budgets import get_budgets, remove_budget, set_budget
from app.tools.reports import daily_breakdown, get_balances, get_transactions, monthly_trend, spending_summary
from app.tools.transactions import add_expenses, add_income, set_account_balance, undo_transaction
from app.tools.transfers import transfer_funds

ALL_TOOLS = [
    respond_to_user,
    add_expenses,
    add_income,
    transfer_funds,
    get_balances,
    get_transactions,
    spending_summary,
    monthly_trend,
    daily_breakdown,
    set_budget,
    get_budgets,
    remove_budget,
    set_account_balance,
    undo_transaction,
]
TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}
