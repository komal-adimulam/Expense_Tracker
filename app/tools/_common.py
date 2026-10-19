"""Helpers shared by all tools."""
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.core.types import AccountType, is_cancel, parse_account

CHOOSE_ACCOUNT = "choose_account"


def user_id_from(config: RunnableConfig) -> int:
    return config["configurable"]["user_id"]


def ask_account(question: str) -> AccountType | None:
    """
    Human-in-the-loop: pause the graph and ask the user cash or bank.
    Returns the chosen account, or None if the user cancelled.

    The AgentRunner already screens the reply (button or free text) before resuming,
    so only 'cash' / 'bank' / 'cancel' reach here.
    NOTE: interrupt() re-runs this tool from the top on resume, so call it BEFORE any DB write.
    """
    answer = interrupt({"type": CHOOSE_ACCOUNT, "question": question, "options": ["cash", "bank"]})
    if is_cancel(answer):
        return None
    return parse_account(str(answer))
