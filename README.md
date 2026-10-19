# Wall - wallet ledger agent for Telegram

Chat naturally ("spent 300 on ice cream", "got salary 50k in bank", "withdrew 2000", "set a limit of 3k for food").
An LLM agent (LangGraph) turns it into ledger entries in PostgreSQL over two accounts: **cash** and **bank**.

## Architecture

```
Telegram ──► bot/handlers ──► agent/runner ──► agent/graph (LangGraph)
 (buttons)     auth, typing       1 thread per       agent(LLM) ⇄ tools (one call per step)
                                  user, locks               │ interrupt() when cash/bank missing
                                       │                    ▼
                          Postgres checkpointer        tools/*  (thin, LLM-facing)
                          (chat memory + paused Qs)         ▼
                                                       core/*   (ledger, budgets, reports - no LLM)
                                                            ▼
                                                       db/*     (SQLAlchemy models, Postgres)
```

| Folder | Role |
|---|---|
| `app/core/` | **Business logic.** `ledger.py` (the only code that moves money), `budgets.py` (limits + alerts), `reports.py`, `periods.py`, `types.py` |
| `app/tools/` | LangChain tools the LLM can call. Add a tool = new function + list it in `tools/__init__.py` |
| `app/agent/` | `llm.py` (provider by API key), `prompts.py`, `graph.py`, `runner.py` |
| `app/bot/` | Telegram handlers, inline keyboards |
| `app/api/` | `/health`, `/telegram/webhook` |
| `app/db/` | models + async session |
| `tests/` | core logic + full agent flow with a scripted fake LLM |

### Key behaviours
- **Ledger:** every transaction has `from_account` and/or `to_account`. Expense = cash/bank → out. Income = in. Transfer (withdraw/deposit) = bank → cash. Balances change in one DB transaction with row locks; overdrafts are refused (`ALLOW_NEGATIVE_BALANCE`). Nothing is deleted: *undo* voids and reverses.
- **Human in the loop:** no "cash"/"bank" in the message → the tool calls `interrupt()`, bot shows **Cash / Bank / Cancel** buttons (typing also works). The pause is stored in Postgres so it survives restarts. An unrelated reply cancels the pending question and is handled as a new request.
- **One tool call per graph step:** a resumed step never repeats a finished write, and several questions never collide.
- **Budgets:** monthly per category. Alerts at 50/80/100% (`BUDGET_ALERT_THRESHOLDS`), each fires once per month. The LLM sees your budget categories, so "ice cream" → `food`.
- **Any LLM key:** `LLM_API_KEY` prefix selects the provider (`AIza` Gemini, `sk-ant-` Anthropic, `gsk_` Groq, `sk-` OpenAI). Override with `LLM_PROVIDER` / `LLM_MODEL`.

## Guardrails (scope)
The agent only handles the user's own wallet. Two layers, so it doesn't depend on the model behaving:
1. **Prompt:** coding, general knowledge, maths, general finance explanations, role-play and "ignore your instructions" are all refused.
2. **Code:** the LLM is never allowed to write free text. Every non-ledger message goes through the `respond_to_user` tool and the wording (refusal / greeting / help) comes from `app/agent/guardrails.py`. If the model answers in prose anyway, the graph replaces it with the refusal. Messages over 1000 characters are rejected.

## Bank-statement PDFs
Send a PDF (caption `password 12345` if it is protected) and the bot:
1. extracts the text (`pypdf`; scanned images are not supported), 2. has the LLM pull out each row and classify it (chunked, 2 at a time, rate-limit backoff; if any chunk fails nothing is imported),
3. validates every date/amount in code and flags rows **already imported** (re-upload safe) and rows that look like entries you typed by hand (same amount, ±1 day),
4. shows a preview with buttons, and only then applies it: debits -> bank expenses, credits -> bank income, ATM withdrawals -> bank → cash transfer.
Buttons after import: **Undo this import** and **Set bank balance to the statement's closing balance**. Statements may overdraw the bank (they are history).

## Month-wise / date-wise
Every transaction stores its full timestamp and category; months and days are computed in your timezone when you ask (a 23:30 expense on the 31st stays in that month).
- **Any period:** `2026-09` (a month), `2026-10-15` (a day), `2026-10-01..2026-10-15` (a range), plus today / this_week / last_30_days / this_month / last_month / this_year / all_time. Try "how much did I spend in september", "spending from 1st to 15th october".
- **Month-by-month trend:** `/trend` or "month wise spending" (3 / 6 / 12 month buttons) with per-category history; "how has my food spending changed" shows one category.
- **Daily breakdown:** `/daily` or "day wise spending for august" with the biggest day and the daily average.
- **Browse in Telegram:** the 📊 Summary message has ◀ / ▶ month buttons plus 📅 Daily and 📈 Trend; budgets of a past month: "show september budgets".

## Speed & UX
- **One LLM call per message.** After a tool runs, the reply is built from its output (`respond` node), not by a second LLM round-trip. Gemini "thinking" is switched off/low.
- **Instant menu** (no LLM at all): 💰 Balance, 🧾 Recent, 📊 Summary (tap Today / Week / Month / Last month), 🎯 Budgets with progress bars, ↩️ Undo last (with confirm).
- **Buttons under results:** ↩️ Undo and 💰 Balance on every recorded entry; Cash / Bank / Cancel when the account is missing.
- Live "typing…" indicator, concurrent users (one slow request never blocks another), `/` command menu.

## Run

1. Telegram: talk to @BotFather → `/newbot` → copy the token. Get your numeric id from @userinfobot.
2. `cp .env.example .env` and fill `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, `ALLOWED_TELEGRAM_IDS`.

**Docker (recommended):** `docker compose up --build`

**Local:** Python 3.12, a Postgres, then
```
pip install -r requirements.txt
python run.py          # not plain uvicorn on Windows - see run.py
```

**Tests** (needs Postgres from `DATABASE_URL`): `pip install -r requirements-dev.txt && pytest`

**Production:** set `TELEGRAM_MODE=webhook`, `PUBLIC_URL=https://your.domain`, a strong `TELEGRAM_WEBHOOK_SECRET`.

## Ideas for next steps
Alembic migrations, recurring entries (rent/EMI), daily/weekly summary push, receipt photo parsing, CSV export, multi-currency.
