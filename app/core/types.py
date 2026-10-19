import re
from enum import Enum


class AccountType(str, Enum):
    CASH = "cash"
    BANK = "bank"


class TxnKind(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    ADJUSTMENT = "adjustment"


_ACCOUNT_WORDS = {
    AccountType.CASH: {"cash", "hand", "wallet", "pocket"},
    AccountType.BANK: {
        "bank", "card", "upi", "online", "gpay", "phonepe", "paytm", "netbanking",
        "account", "debit", "credit", "atm", "neft", "imps",
    },
}
_CANCEL_WORDS = {"cancel", "skip", "nevermind", "stop", "no", "ignore", "forget"}


def parse_account(text: str | None) -> AccountType | None:
    """'UPI' -> BANK, 'in hand' -> CASH. None when absent or ambiguous."""
    if not text:
        return None
    words = set(re.findall(r"[a-z]+", text.lower()))
    hits = {acc for acc, vocab in _ACCOUNT_WORDS.items() if words & vocab}
    return hits.pop() if len(hits) == 1 else None


def is_cancel(text: str | None) -> bool:
    words = set(re.findall(r"[a-z]+", (text or "").lower().replace("never mind", "nevermind")))
    return bool(words & _CANCEL_WORDS) and parse_account(text) is None
