import re

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.agent.runner import AgentRunner
from app.bot.handlers import BotHandlers
from app.bot.keyboards import MENU
from app.config import settings

COMMANDS = [
    BotCommand("balance", "💰 Cash & bank balance"),
    BotCommand("recent", "🧾 Latest transactions"),
    BotCommand("summary", "📊 Spending summary (browse months)"),
    BotCommand("daily", "📅 Day-by-day spending"),
    BotCommand("trend", "📈 Month-by-month trend"),
    BotCommand("budgets", "🎯 Budget progress"),
    BotCommand("undo", "↩️ Undo last transaction"),
    BotCommand("help", "❓ What can I say?"),
]


def build_telegram_app(runner: AgentRunner, llm) -> Application:
    token = settings.telegram_bot_token.strip().strip("\"'")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty. Create a bot with @BotFather and put the token in .env")
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN looks malformed. It must be '<digits>:<secret>', e.g. 1234567890:AAE... "
            "Copy the full token from @BotFather (/mybots -> API Token)."
        )
    h = BotHandlers(runner, llm)
    # concurrent_updates: one user's slow LLM call must not block everybody else
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", h.start))
    app.add_handler(CommandHandler("help", h.help))
    app.add_handler(CommandHandler(["balance", "budgets", "recent", "summary", "daily", "trend", "undo"], h.on_command))
    app.add_handler(CallbackQueryHandler(h.on_callback))
    menu_regex = "^(" + "|".join(re.escape(label) for label in MENU) + ")$"
    app.add_handler(MessageHandler(filters.Regex(menu_regex), h.on_menu))
    app.add_handler(MessageHandler(filters.Document.ALL, h.on_document))
    app.add_handler(MessageHandler(filters.PHOTO, h.on_other_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_text))
    return app
