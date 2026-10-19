"""
Bank-statement import (everything except the LLM, which lives in app/agent/statement_parser.py).

    PDF bytes --extract_pdf_pages--> text --(LLM)--> raw rows --build_plan--> items (new / duplicate / maybe)
        --save_import--> StatementImport(pending) --user taps confirm--> apply_import --> ledger entries on BANK
                                                                      |--> undo_import reverses them all
All amounts are validated here in code: the LLM output is never trusted blindly.
"""
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import budgets, ledger
from app.core.formatting import balances_line, cat_icon, money
from app.core.periods import now_local, to_local, tz
from app.core.types import AccountType, TxnKind
from app.db.models import StatementImport, Transaction

MAX_PAGES = 80
MAX_ROWS = 1500
CATEGORIES = [
    "food", "groceries", "transport", "fuel", "shopping", "clothes", "bills", "rent", "emi", "entertainment",
    "health", "education", "travel", "insurance", "investment", "cash_withdrawal", "salary", "interest",
    "refund", "other",
]
CASH_WITHDRAWAL = "cash_withdrawal"


class StatementError(Exception):
    """User-facing problem with the uploaded statement."""


class PasswordRequired(StatementError):
    pass


class WrongPassword(StatementError):
    pass


# ------------------------------------------------------------------ PDF -> text
def extract_pdf_pages(data: bytes, password: str | None = None) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        raise StatementError("I couldn't open that file - is it a valid PDF?") from None
    if reader.is_encrypted:
        if not password:
            raise PasswordRequired("This PDF is password protected.")
        try:
            ok = reader.decrypt(password)
        except Exception:
            ok = 0
        if not ok:
            raise WrongPassword("That password didn't work.")
    if len(reader.pages) > MAX_PAGES:
        raise StatementError(f"That statement has {len(reader.pages)} pages; I handle up to {MAX_PAGES}. Send it in parts.")
    try:
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception:
        raise StatementError("I couldn't read the text in that PDF.") from None
    if not "".join(pages).strip():
        raise StatementError("That PDF has no readable text (probably a scan/photo). Please download the digital statement from your bank.")
    return pages


# ------------------------------------------------------------------ raw rows -> plan
def _norm_desc(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _clean_row(raw: dict) -> dict | None:
    """Validate one LLM row. Returns None if it can't be trusted."""
    try:
        d = datetime.strptime(str(raw.get("date", "")).strip()[:10], "%Y-%m-%d").date()
        amount = Decimal(str(raw.get("amount"))).copy_abs().quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation, TypeError):
        return None
    kind = str(raw.get("type", "")).strip().lower()
    if kind not in ("debit", "credit") or amount <= 0 or d > now_local().date() + timedelta(days=1) or d.year < 2000:
        return None
    category = ledger.normalize_category(raw.get("category"))
    desc = re.sub(r"\s+", " ", str(raw.get("description") or "")).strip()[:120] or category
    bal = raw.get("balance_after")
    try:
        bal = str(Decimal(str(bal)).quantize(Decimal("0.01"))) if bal is not None else None
    except InvalidOperation:
        bal = None
    return {"date": d.isoformat(), "description": desc, "amount": str(amount), "type": kind,
            "category": category, "balance_after": bal}


def _closing_balance(rows: list[dict]) -> str | None:
    with_bal = [r for r in rows if r["balance_after"] is not None]
    if len(with_bal) < 2:
        return with_bal[0]["balance_after"] if with_bal else None
    newest_last = with_bal[-1]["date"] >= with_bal[0]["date"]  # statement order: oldest->newest or reverse
    return (with_bal[-1] if newest_last else with_bal[0])["balance_after"]


async def build_plan(session: AsyncSession, user_id: int, raw_rows: list[dict]) -> tuple[list[dict], dict]:
    rows = [r for r in (_clean_row(x) for x in raw_rows) if r]
    dropped = len(raw_rows) - len(rows)
    if not rows:
        raise StatementError("I couldn't find any transactions in that PDF. I can only import bank/card statements.")
    if len(rows) > MAX_ROWS:
        raise StatementError(f"That statement has {len(rows)} rows; the limit is {MAX_ROWS}. Send it in parts.")
    closing = _closing_balance(rows)

    seen: dict[str, int] = {}
    for r in rows:  # identical rows on the same day get distinct keys (two coffees of 100 are two coffees)
        base = f"{user_id}|{r['date']}|{r['type']}|{r['amount']}|{_norm_desc(r['description'])}"
        n = seen[base] = seen.get(base, 0) + 1
        r["key"] = hashlib.sha1(f"{base}|{n}".encode()).hexdigest()[:40]

    already = set((await session.execute(
        select(Transaction.external_key).where(
            Transaction.user_id == user_id, Transaction.voided_at.is_(None),
            Transaction.external_key.in_([r["key"] for r in rows]),
        )
    )).scalars().all())

    lo = date.fromisoformat(min(r["date"] for r in rows)) - timedelta(days=1)
    hi = date.fromisoformat(max(r["date"] for r in rows)) + timedelta(days=1)
    manual = list((await session.execute(
        select(Transaction).where(
            Transaction.user_id == user_id, Transaction.voided_at.is_(None), Transaction.external_key.is_(None),
            Transaction.occurred_at >= datetime.combine(lo, time.min, tzinfo=tz()),
            Transaction.occurred_at < datetime.combine(hi + timedelta(days=1), time.min, tzinfo=tz()),
            Transaction.kind.in_([TxnKind.EXPENSE.value, TxnKind.INCOME.value, TxnKind.TRANSFER.value]),
        )
    )).scalars().all())

    for r in rows:
        if r["key"] in already:
            r["status"] = "duplicate"
            continue
        r["status"] = "new"
        d, amount = date.fromisoformat(r["date"]), Decimal(r["amount"])
        for m in manual:  # did the user already log this by hand? (same amount, bank side, +-1 day)
            bank_side = (m.from_account if r["type"] == "debit" else m.to_account) == AccountType.BANK.value
            if m.amount == amount and bank_side and abs((to_local(m.occurred_at).date() - d).days) <= 1:
                manual.remove(m)
                r["status"] = "maybe"
                break

    debits = [Decimal(r["amount"]) for r in rows if r["type"] == "debit"]
    credits = [Decimal(r["amount"]) for r in rows if r["type"] == "credit"]
    meta = {
        "count": len(rows), "dropped": dropped,
        "start": min(r["date"] for r in rows), "end": max(r["date"] for r in rows),
        "debit_total": str(sum(debits, Decimal(0))), "credit_total": str(sum(credits, Decimal(0))),
        "closing_balance": closing,
    }
    return rows, meta


async def save_import(session: AsyncSession, user_id: int, filename: str | None, items: list[dict], meta: dict) -> StatementImport:
    imp = StatementImport(user_id=user_id, filename=(filename or "statement.pdf")[:256], items=items, meta=meta)
    session.add(imp)
    await session.flush()
    return imp


# ------------------------------------------------------------------ preview
def counts(items: list[dict]) -> dict[str, int]:
    return {s: sum(1 for i in items if i["status"] == s) for s in ("new", "maybe", "duplicate")}


def preview_text(imp: StatementImport) -> str:
    items, meta = imp.items, imp.meta
    c = counts(items)
    fresh = [i for i in items if i["status"] != "duplicate"]
    debit = sum((Decimal(i["amount"]) for i in fresh if i["type"] == "debit"), Decimal(0))
    credit = sum((Decimal(i["amount"]) for i in fresh if i["type"] == "credit"), Decimal(0))
    fmt = lambda s: date.fromisoformat(s).strftime("%d %b %Y")
    lines = [
        f"📄 {imp.filename}",
        f"🗓 {fmt(meta['start'])} → {fmt(meta['end'])} · {meta['count']} transactions\n",
        f"🔴 Money out  {money(debit)}",
        f"🟢 Money in   {money(credit)}",
        f"🏦 Net change to your bank: {'+' if credit >= debit else ''}{money(credit - debit)}\n",
    ]
    by_cat: dict[str, Decimal] = {}
    for i in fresh:
        if i["type"] == "debit":
            by_cat[i["category"]] = by_cat.get(i["category"], Decimal(0)) + Decimal(i["amount"])
    if by_cat:
        lines.append("Spending by category")
        total = sum(by_cat.values()) or Decimal(1)
        for cat, amt in sorted(by_cat.items(), key=lambda kv: -kv[1])[:8]:
            tag = " → moved to cash" if cat == CASH_WITHDRAWAL else ""
            lines.append(f"{cat_icon(cat)} {cat:<15} {money(amt)} ({int(amt * 100 / total)}%){tag}")
        lines.append("")
    if c["duplicate"]:
        lines.append(f"♻️ {c['duplicate']} already imported before - skipped")
    if c["maybe"]:
        lines.append(f"❓ {c['maybe']} look like entries you added by hand (same amount, ±1 day) - skipped unless you include them")
    if meta.get("dropped"):
        lines.append(f"⚠️ {meta['dropped']} unreadable rows ignored")
    lines.append(f"✅ Ready to import {c['new']} new transactions.")
    return "\n".join(lines)


# ------------------------------------------------------------------ apply / undo
@dataclass
class ImportResult:
    applied: int
    text: str
    closing_balance: Decimal | None


def _occurred(d: date) -> datetime | None:
    return None if d == now_local().date() else datetime.combine(d, time(12, 0), tzinfo=tz())


async def _load_pending(session: AsyncSession, user_id: int, import_id: int, want: str) -> StatementImport:
    imp = (await session.execute(
        select(StatementImport).where(StatementImport.id == import_id, StatementImport.user_id == user_id).with_for_update()
    )).scalar_one_or_none()
    if imp is None:
        raise StatementError("I can't find that import.")
    if imp.status != want:
        raise StatementError({"done": "That statement was already imported.", "cancelled": "That import was cancelled.",
                              "undone": "That import was already undone.", "pending": "That import isn't done yet."}[imp.status])
    return imp


async def apply_import(session: AsyncSession, user_id: int, import_id: int, include_maybes: bool = False) -> ImportResult:
    imp = await _load_pending(session, user_id, import_id, "pending")  # row lock + status flip = tap-twice safe
    wanted = {"new", "maybe"} if include_maybes else {"new"}
    todo = sorted((i for i in imp.items if i["status"] in wanted), key=lambda i: i["date"])
    this_month = now_local().strftime("%Y-%m")
    spent_categories: set[str] = set()

    for i in todo:
        amount, when, desc = Decimal(i["amount"]), _occurred(date.fromisoformat(i["date"])), i["description"]
        common = dict(user_id=user_id, amount=amount, description=desc, occurred_at=when,
                      external_key=i["key"], import_id=imp.id, allow_negative=True)  # statements are history: never refuse
        if i["type"] == "credit":
            await ledger.post(session, kind=TxnKind.INCOME, to_account=AccountType.BANK, category=i["category"], **common)
        elif i["category"] == CASH_WITHDRAWAL:
            await ledger.post(session, kind=TxnKind.TRANSFER, from_account=AccountType.BANK,
                              to_account=AccountType.CASH, category="transfer", **common)
        else:
            await ledger.post(session, kind=TxnKind.EXPENSE, from_account=AccountType.BANK, category=i["category"], **common)
            if i["date"][:7] == this_month:
                spent_categories.add(i["category"])

    imp.status = "done"
    imp.meta = {**imp.meta, "applied": len(todo)}
    alerts = [a for c in sorted(spent_categories) if (a := await budgets.evaluate_alerts(session, user_id, c))]
    bal = balances_line(await ledger.get_balances(session, user_id))
    text = "\n".join([f"✅ Imported {len(todo)} transactions from {imp.filename}", *alerts, "", bal])
    closing = imp.meta.get("closing_balance")
    return ImportResult(len(todo), text, Decimal(closing) if closing else None)


async def cancel_import(session: AsyncSession, user_id: int, import_id: int) -> None:
    imp = await _load_pending(session, user_id, import_id, "pending")
    imp.status = "cancelled"


async def undo_import(session: AsyncSession, user_id: int, import_id: int) -> str:
    imp = await _load_pending(session, user_id, import_id, "done")
    ids = (await session.execute(
        select(Transaction.id).where(Transaction.import_id == imp.id, Transaction.voided_at.is_(None)).order_by(Transaction.id)
    )).scalars().all()
    for tid in ids:
        await ledger.void_transaction(session, user_id, tid)
    imp.status = "undone"
    bal = balances_line(await ledger.get_balances(session, user_id))
    return f"↩️ Import undone - {len(ids)} transactions reversed.\n\n{bal}"


async def set_bank_to_closing(session: AsyncSession, user_id: int, import_id: int) -> str:
    imp = (await session.execute(
        select(StatementImport).where(StatementImport.id == import_id, StatementImport.user_id == user_id)
    )).scalar_one_or_none()
    closing = imp.meta.get("closing_balance") if imp else None
    if not closing:
        raise StatementError("That statement has no closing balance.")
    await ledger.set_balance(session, user_id, AccountType.BANK, Decimal(closing))
    return f"🏦 Bank balance set to {money(Decimal(closing))} (statement closing).\n\n" + balances_line(await ledger.get_balances(session, user_id))
