import asyncio
import logging
import re
from contextlib import asynccontextmanager

from sqlalchemy import select
from telegram import Update
from telegram import User as TgUser
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.agent import statement_parser
from app.agent.guardrails import HELP, MAX_USER_MESSAGE_CHARS
from app.agent.runner import AgentReply, AgentRunner, is_rate_limit
from app.bot import keyboards as kb
from app.config import settings
from app.core import budgets, ledger, statements, views
from app.core.formatting import balances_line, friendly_txn, money
from app.core.periods import PeriodError, month_key
from app.core.ledger import LedgerError
from app.db.models import Transaction
from app.db.session import session_scope

log = logging.getLogger(__name__)
REPLY_TIMEOUT = 60  # hard cap on one message, whatever the LLM does

MAX_PDF_BYTES = 10 * 1024 * 1024
STATEMENT_TIMEOUT = 180


@asynccontextmanager
async def keep_typing(bot, chat_id: int):
    """Telegram's 'typing…' fades after ~5s, so refresh it until the answer is ready."""
    async def loop():
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


class BotHandlers:
    def __init__(self, runner: AgentRunner, llm):
        self.runner = runner
        self.llm = llm  # used by the statement importer
        self._user_ids: dict[int, int] = {}  # telegram id -> db id (saves a query per message)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _allowed(tg: TgUser) -> bool:
        return not settings.allowed_ids or tg.id in settings.allowed_ids

    async def _guard(self, update: Update) -> bool:
        if self._allowed(update.effective_user):
            return True
        target = update.effective_message or (update.callback_query and update.callback_query.message)
        if target:
            await target.reply_text("Sorry, this is a private bot. 🔒")
        return False

    async def _uid(self, tg: TgUser) -> int:
        if tg.id not in self._user_ids:
            async with session_scope() as s:
                self._user_ids[tg.id] = (await ledger.get_or_create_user(s, tg.id, tg.full_name)).id
        return self._user_ids[tg.id]

    async def _ask_agent(self, tg: TgUser, text: str, from_button: bool = False) -> AgentReply:
        try:
            return await asyncio.wait_for(
                self.runner.chat(user_id=await self._uid(tg), thread_id=f"tg-{tg.id}", text=text, from_button=from_button),
                timeout=REPLY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.error("agent timed out for telegram user %s", tg.id)
            return AgentReply("⏱️ The AI took too long to answer. Please send it again.")
        except Exception as e:
            if is_rate_limit(e):
                log.warning("LLM rate limit hit for telegram user %s", tg.id)
                return AgentReply("⏳ The AI is a bit busy right now (rate limit). Please try again in about a minute.")
            log.exception("agent failed for telegram user %s", tg.id)
            return AgentReply("😵 Sorry, my brain hiccuped (AI or database error). Please try again in a moment.")

    @staticmethod
    async def _send(message, reply: AgentReply) -> None:
        if reply.choices:
            markup = kb.choice_keyboard(reply.choices)
        elif reply.txn_ids:
            markup = kb.result_keyboard(reply.txn_ids)
        else:
            markup = None
        await message.reply_text(reply.text[:4000], reply_markup=markup)

    # ------------------------------------------------------------ commands + menu (no LLM, instant)
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await self._uid(update.effective_user)
            await update.effective_message.reply_text(HELP, reply_markup=kb.main_menu())

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await update.effective_message.reply_text(HELP, reply_markup=kb.main_menu())

    async def _direct(self, tg: TgUser, message, action: str) -> None:
        uid = await self._uid(tg)
        async with session_scope() as s:
            if action == "balance":
                await message.reply_text(await views.balance_text(s, uid))
            elif action == "budgets":
                await message.reply_text(await views.budgets_text(s, uid))
            elif action == "recent":
                await message.reply_text(await views.recent_text(s, uid, limit=10))
            elif action == "summary":
                await message.reply_text(await views.summary_text(s, uid, "this_month"), reply_markup=kb.summary_keyboard("this_month"))
            elif action == "daily":
                await message.reply_text(await views.daily_text(s, uid), reply_markup=kb.daily_keyboard(month_key()))
            elif action == "trend":
                await message.reply_text(await views.trend_text(s, uid, 6), reply_markup=kb.trend_keyboard(6))
            elif action == "undo":
                last = (await s.execute(
                    select(Transaction).where(Transaction.user_id == uid, Transaction.voided_at.is_(None))
                    .order_by(Transaction.id.desc()).limit(1)
                )).scalar_one_or_none()
                if last is None:
                    await message.reply_text("Nothing to undo yet.")
                else:
                    await message.reply_text(
                        f"Undo this?\n\n{friendly_txn(last)}", reply_markup=kb.undo_confirm_keyboard(last.id)
                    )

    async def on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/balance /budgets /recent /summary /undo"""
        if await self._guard(update):
            action = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
            await self._direct(update.effective_user, update.effective_message, action)

    async def on_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        action = kb.MENU[update.effective_message.text]
        if action == "help":
            await self.help(update, context)
        else:
            await self._direct(update.effective_user, update.effective_message, action)

    # ------------------------------------------------------------ free text -> agent
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg = update.effective_message
        if len(msg.text) > MAX_USER_MESSAGE_CHARS:
            await msg.reply_text("✂️ That message is too long for me. Please keep it short, e.g. \"spent 200 on lunch\".")
            return
        async with keep_typing(context.bot, msg.chat_id):
            reply = await self._ask_agent(update.effective_user, msg.text)
        await self._send(msg, reply)

    # ------------------------------------------------------------ inline buttons
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()  # stops the button's loading spinner immediately
        if not await self._guard(update):
            return
        kind, _, arg = q.data.partition(":")
        tg, msg = q.from_user, q.message

        if kind == "hitl":  # answer to "cash or bank?"
            await q.edit_message_reply_markup(reply_markup=None)
            async with keep_typing(context.bot, msg.chat_id):
                reply = await self._ask_agent(tg, arg, from_button=True)
            await self._send(msg, reply)

        elif kind == "undo":
            await self._undo(q, tg, [int(i) for i in arg.split(",") if i])

        elif kind == "keep":
            await q.edit_message_text("👍 Kept.")

        elif kind == "menu":
            await self._direct(tg, msg, arg)

        elif kind == "imp":
            await self._import_action(q, tg, arg)

        elif kind == "impundo":
            await self._import_undo(q, tg, int(arg))

        elif kind == "impbal":
            await self._import_set_balance(q, tg, int(arg))

        elif kind in ("sum", "day", "trend"):
            await self._view(q, tg, kind, arg)

    async def _undo(self, q, tg: TgUser, ids: list[int]) -> None:
        uid, lines = await self._uid(tg), []
        try:
            async with session_scope() as s:
                for i in ids:
                    lines.append(f"↩️ Undone: {friendly_txn(await ledger.void_transaction(s, uid, i))}")
                bal = balances_line(await ledger.get_balances(s, uid))
        except LedgerError:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("That was already undone.")
            return
        await q.edit_message_text("\n".join([*lines, "", bal]))

    # ------------------------------------------------------------ bank statement PDF
    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg, doc = update.effective_message, update.effective_message.document
        is_pdf = doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")
        if not is_pdf:
            await msg.reply_text("📄 I can only read PDF bank statements. Please send a PDF.")
            return
        if doc.file_size and doc.file_size > MAX_PDF_BYTES:
            await msg.reply_text("📄 That PDF is bigger than 10 MB. Please send a smaller one.")
            return

        pw = re.search(r"(?i)pass(?:word)?\s*[:=]?\s*(\S+)", msg.caption or "")
        status = await msg.reply_text("📄 Got your statement - reading it. This can take up to a minute…")
        uid = await self._uid(update.effective_user)
        try:
            async with keep_typing(context.bot, msg.chat_id):
                data = bytes(await (await doc.get_file()).download_as_bytearray())
                pages = await asyncio.to_thread(statements.extract_pdf_pages, data, pw.group(1) if pw else None)
                async with session_scope() as s:
                    cats = await budgets.budget_categories(s, uid)
                rows = await asyncio.wait_for(statement_parser.parse_pages(self.llm, pages, cats), STATEMENT_TIMEOUT)
                async with session_scope() as s:
                    items, meta = await statements.build_plan(s, uid, rows)
                    imp = await statements.save_import(s, uid, doc.file_name, items, meta)
            c = statements.counts(imp.items)
            await status.edit_text(statements.preview_text(imp), reply_markup=kb.import_keyboard(imp.id, c["new"], c["maybe"]))
        except statements.PasswordRequired:
            await status.edit_text("🔒 This PDF is password protected. Send it again with the password as the caption, e.g.  password 12345")
        except statements.WrongPassword:
            await status.edit_text("🔒 That password didn't work. Send the PDF again with the right password as the caption.")
        except statements.StatementError as e:
            await status.edit_text(f"❌ {e}")
        except asyncio.TimeoutError:
            await status.edit_text("⏱️ Reading the statement took too long. Please try again, or send a shorter period.")
        except Exception:
            log.exception("statement import failed for telegram user %s", update.effective_user.id)
            await status.edit_text("😵 Sorry, I couldn't process that statement. Please try again.")

    async def on_other_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await update.effective_message.reply_text("📄 I can only read PDF bank statements. Please send a PDF.")

    async def _import_action(self, q, tg: TgUser, arg: str) -> None:
        raw_id, _, mode = arg.partition(":")
        uid = await self._uid(tg)
        try:
            async with session_scope() as s:
                if mode == "no":
                    await statements.cancel_import(s, uid, int(raw_id))
                    await q.edit_message_text("👍 Cancelled - nothing was imported.")
                    return
                res = await statements.apply_import(s, uid, int(raw_id), include_maybes=(mode == "all"))
        except statements.StatementError as e:
            await q.edit_message_text(f"ℹ️ {e}")
            return
        closing = money(res.closing_balance) if res.closing_balance is not None else None
        await q.edit_message_text(res.text, reply_markup=kb.after_import_keyboard(int(raw_id), closing))

    async def _import_undo(self, q, tg: TgUser, import_id: int) -> None:
        try:
            async with session_scope() as s:
                text = await statements.undo_import(s, await self._uid(tg), import_id)
        except (statements.StatementError, LedgerError) as e:
            await q.edit_message_text(f"ℹ️ {e}")
            return
        await q.edit_message_text(text)

    async def _import_set_balance(self, q, tg: TgUser, import_id: int) -> None:
        try:
            async with session_scope() as s:
                text = await statements.set_bank_to_closing(s, await self._uid(tg), import_id)
        except statements.StatementError as e:
            await q.message.reply_text(f"ℹ️ {e}")
            return
        await q.message.reply_text(text)

    async def _view(self, q, tg: TgUser, kind: str, arg: str) -> None:
        """Summary / daily / trend screens that edit one message in place (month navigation, range switch)."""
        uid = await self._uid(tg)
        try:
            async with session_scope() as s:
                if kind == "sum":
                    text, markup = await views.summary_text(s, uid, arg), kb.summary_keyboard(arg)
                elif kind == "day":
                    text, markup = await views.daily_text(s, uid, arg), kb.daily_keyboard(arg)
                else:
                    text, markup = await views.trend_text(s, uid, int(arg)), kb.trend_keyboard(int(arg))
            await q.edit_message_text(text, reply_markup=markup)
        except BadRequest:  # same content tapped twice
            pass
        except PeriodError as e:
            await q.message.reply_text(f"❌ {e}")
