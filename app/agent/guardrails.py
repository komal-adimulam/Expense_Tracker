"""
Scope guardrails. The LLM never writes free-form answers: for anything that is not a ledger action it
must call `respond_to_user`, and the wording of those replies comes from the constants below, not from the model.
"""

SCOPE_MESSAGE = (
    "🙅 That's outside my job - I only handle your wallet:\n"
    "spending, income, cash ⇄ bank moves, budgets, balances, history and bank-statement PDFs.\n\n"
    "Try: spent 200 on lunch"
)

GREETING = "👋 Hi! Tell me what you spent or received, e.g. \"spent 200 on lunch\", or send a bank-statement PDF."

HELP = (
    "👋 I'm Wall, your wallet ledger. Just talk to me:\n\n"
    "🔴 spent 300 on ice cream\n"
    "🟢 got stipend 15k in bank\n"
    "🔁 withdrew 2000 from ATM\n"
    "🎯 set a limit of 3k for food\n"
    "📊 how much did I spend this month?\n"
    "↩️ undo last\n"
    "📄 send a bank-statement PDF to import it\n\n"
    "If you don't say cash or bank, I'll ask with buttons.\n"
    "Use the menu below for instant answers."
)

MAX_CLARIFY_CHARS = 200
MAX_USER_MESSAGE_CHARS = 1000


def render_reply(kind: str, message: str | None) -> str:
    kind = (kind or "").strip().lower()
    if kind == "greeting":
        return GREETING
    if kind == "help":
        return HELP
    if kind == "clarify" and message:
        return "❓ " + message.strip()[:MAX_CLARIFY_CHARS]
    return SCOPE_MESSAGE
