"""Pure text rendering. Everything the user reads is built here so tools and bot look the same."""
from decimal import Decimal

from app.config import settings
from app.core.periods import to_local
from app.db.models import Transaction

BAL_PREFIX = "💰"  # the respond node keeps only the LAST line starting with this

ACCOUNT_ICON = {"cash": "💵", "bank": "🏦"}
CATEGORY_ICON = {
    "food": "🍔", "groceries": "🛒", "transport": "🚌", "travel": "✈️", "fuel": "⛽",
    "clothes": "👕", "shopping": "🛍️", "bills": "🧾", "rent": "🏠", "entertainment": "🎬",
    "health": "💊", "education": "📚", "salary": "💼", "stipend": "💼", "refund": "↩️",
    "gift": "🎁", "transfer": "🔁", "income": "💵", "insurance": "🛡️", "cash_withdrawal": "🏧",
    "emi": "💳", "investment": "📈", "interest": "💹",
}


def money(value: Decimal | float | int) -> str:
    d = Decimal(value).quantize(Decimal("0.01"))
    text = f"{abs(d):,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    sign = "-" if d < 0 else ""
    return f"{sign}{settings.currency_symbol}{text}"


def cat_icon(category: str | None) -> str:
    return CATEGORY_ICON.get((category or "").lower(), "🏷️")


def acct(name: str | None) -> str:
    return f"{ACCOUNT_ICON.get(name or '', '')} {name}".strip()


def bar(percent: int, width: int = 10) -> str:
    filled = min(width, round(width * percent / 100))
    return "▓" * filled + "░" * (width - filled)


def status_dot(percent: int) -> str:
    return "🟢" if percent < 50 else "🟡" if percent < 80 else "🟠" if percent < 100 else "🔴"


def balances_line(balances: dict[str, Decimal]) -> str:
    cash, bank = balances.get("cash", Decimal(0)), balances.get("bank", Decimal(0))
    return f"{BAL_PREFIX} Cash {money(cash)}  ·  🏦 Bank {money(bank)}  ·  Total {money(cash + bank)}"


def balance_card(balances: dict[str, Decimal]) -> str:
    cash, bank = balances.get("cash", Decimal(0)), balances.get("bank", Decimal(0))
    return f"👛 Your wallet\n\n💵 Cash   {money(cash)}\n🏦 Bank   {money(bank)}\n━━━━━━━━━━━━\nTotal   {money(cash + bank)}"


def friendly_txn(t: Transaction, prefix: str = "") -> str:
    """One confirmation line for a freshly recorded transaction."""
    label = t.description or t.category or t.kind
    if t.kind == "expense":
        head = f"🔴 Spent {money(t.amount)} on {label} {cat_icon(t.category)} from {acct(t.from_account)}"
    elif t.kind == "income":
        head = f"🟢 Received {money(t.amount)} · {label} to {acct(t.to_account)}"
    elif t.kind == "transfer":
        head = f"🔁 Moved {money(t.amount)} from {acct(t.from_account)} to {acct(t.to_account)}"
    else:
        side = f"to {acct(t.to_account)}" if t.to_account else f"from {acct(t.from_account)}"
        head = f"✏️ Adjusted {money(t.amount)} {side}"
    return f"{prefix}{head}  (#{t.id})"


def history_line(t: Transaction) -> str:
    when = to_local(t.occurred_at).strftime("%d %b")
    sign = {"expense": "🔴 -", "income": "🟢 +", "transfer": "🔁 ", "adjustment": "✏️ "}.get(t.kind, "")
    where = (
        f"{t.from_account}→{t.to_account}" if t.from_account and t.to_account
        else (t.from_account or t.to_account or "")
    )
    what = " · ".join(x for x in (t.category, t.description if t.description != t.category else None) if x)
    void = "  ❌ undone" if t.voided_at else ""
    return f"{when}  {sign}{money(t.amount)}  {what}  [{where}]  #{t.id}{void}"
