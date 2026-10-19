import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pypdf import PdfWriter

from app.agent import statement_parser

statement_parser.BACKOFF = (0, 0, 0)  # no real waiting in tests
from app.core import ledger, statements
from app.core.periods import now_local
from app.core.statements import PasswordRequired, StatementError, WrongPassword
from app.core.types import AccountType
from app.db.session import session_scope

BANK, CASH = AccountType.BANK, AccountType.CASH
TODAY = date.today()


def d(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def row(days_ago, desc, amount, kind, category, bal=None):
    return {"date": d(days_ago), "description": desc, "amount": amount, "type": kind, "category": category, "balance_after": bal}


RAW = [
    row(5, "ACME LTD SALARY", 50000, "credit", "salary", 60000),
    row(4, "SWIGGY ORDER", 450.5, "debit", "food", 59549.5),
    row(3, "ATM WDL", 2000, "debit", "cash_withdrawal", 57549.5),
    row(2, "UBER TRIP", 300, "debit", "transport", 57249.5),
    {"date": "garbage", "description": "bad", "amount": 5, "type": "debit", "category": "x"},   # dropped
    {"date": d(1), "description": "neg", "amount": 5, "type": "sideways", "category": "x"},    # dropped
]


async def _plan_and_save(uid, raw=RAW):
    async with session_scope() as s:
        items, meta = await statements.build_plan(s, uid, raw)
        return await statements.save_import(s, uid, "oct.pdf", items, meta)


async def _bal(uid):
    async with session_scope() as s:
        return await ledger.get_balances(s, uid)


async def test_plan_drops_untrustworthy_rows_and_finds_closing_balance(user_id):
    imp = await _plan_and_save(user_id)
    assert imp.meta["count"] == 4 and imp.meta["dropped"] == 2
    assert imp.meta["closing_balance"] == "57249.50"
    assert all(i["status"] == "new" for i in imp.items)
    assert "Import" not in statements.preview_text(imp) and "oct.pdf" in statements.preview_text(imp)


async def test_import_credits_debits_and_atm_moves_to_cash(user_id):
    imp = await _plan_and_save(user_id)
    async with session_scope() as s:
        res = await statements.apply_import(s, user_id, imp.id)
    assert res.applied == 4
    bal = await _bal(user_id)
    # bank: +50000 -450.50 -2000 -300 ; cash: +2000 from the ATM
    assert bal["bank"] == Decimal("47249.50") and bal["cash"] == Decimal("2000")


async def test_reupload_is_skipped_and_double_tap_is_safe(user_id):
    imp = await _plan_and_save(user_id)
    async with session_scope() as s:
        await statements.apply_import(s, user_id, imp.id)
    with pytest.raises(StatementError):  # tapping Import twice
        async with session_scope() as s:
            await statements.apply_import(s, user_id, imp.id)

    again = await _plan_and_save(user_id)
    assert statements.counts(again.items) == {"new": 0, "maybe": 0, "duplicate": 4}


async def test_entries_added_by_hand_are_flagged_as_maybe(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 5000, "x", "seed", BANK)
        await ledger.record_expense(s, user_id, 300, "transport", "uber", BANK, now_local() - timedelta(days=2))  # same as the statement's UBER row
    imp = await _plan_and_save(user_id)
    c = statements.counts(imp.items)
    assert c == {"new": 3, "maybe": 1, "duplicate": 0}

    async with session_scope() as s:
        res = await statements.apply_import(s, user_id, imp.id)  # default: skip the maybe
    assert res.applied == 3


async def test_include_maybes(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 5000, "x", "seed", BANK)
        await ledger.record_expense(s, user_id, 300, "transport", "uber", BANK, now_local() - timedelta(days=2))
    imp = await _plan_and_save(user_id)
    async with session_scope() as s:
        assert (await statements.apply_import(s, user_id, imp.id, include_maybes=True)).applied == 4


async def test_undo_import_restores_balances(user_id):
    async with session_scope() as s:
        await ledger.record_income(s, user_id, 1000, "x", "seed", BANK)
    imp = await _plan_and_save(user_id)
    async with session_scope() as s:
        await statements.apply_import(s, user_id, imp.id)
    async with session_scope() as s:
        msg = await statements.undo_import(s, user_id, imp.id)
    assert "4 transactions reversed" in msg
    bal = await _bal(user_id)
    assert bal["bank"] == Decimal("1000") and bal["cash"] == 0


async def test_statement_may_overdraw_bank_without_failing(user_id):
    imp = await _plan_and_save(user_id, [row(2, "RENT", 9000, "debit", "rent")])  # bank balance is 0
    async with session_scope() as s:
        await statements.apply_import(s, user_id, imp.id)
    assert (await _bal(user_id))["bank"] == Decimal("-9000")


async def test_set_bank_to_closing_balance(user_id):
    imp = await _plan_and_save(user_id)
    async with session_scope() as s:
        await statements.apply_import(s, user_id, imp.id)
    async with session_scope() as s:
        await statements.set_bank_to_closing(s, user_id, imp.id)
    assert (await _bal(user_id))["bank"] == Decimal("57249.50")


async def test_nothing_found_is_a_clear_error(user_id):
    with pytest.raises(StatementError):
        await _plan_and_save(user_id, [{"date": "x"}])


# ------------------------------------------------------------------ PDF + parser
def make_pdf(text: str, password: str | None = None) -> bytes:
    """Smallest valid one-page PDF containing `text` (or an encrypted blank page)."""
    if password:
        w = PdfWriter()
        w.add_blank_page(200, 200)
        w.encrypt(password)
        buf = io.BytesIO()
        w.write(buf)
        return buf.getvalue()
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return out


def test_extract_text_from_pdf():
    pages = statements.extract_pdf_pages(make_pdf("12/10/2026 SWIGGY 450.50 Dr"))
    assert "SWIGGY" in pages[0]


def test_garbage_is_not_a_pdf():
    with pytest.raises(StatementError):
        statements.extract_pdf_pages(b"hello")


def test_password_protected_pdf():
    data = make_pdf("", password="secret123")
    with pytest.raises(PasswordRequired):
        statements.extract_pdf_pages(data)
    with pytest.raises(WrongPassword):
        statements.extract_pdf_pages(data, "nope")
    with pytest.raises(StatementError, match="no readable text"):  # right password -> opens, but page is blank
        statements.extract_pdf_pages(data, "secret123")


class _FakeStructured:
    def __init__(self, fail_times=0):
        self.calls, self.fail_times = 0, fail_times

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        n = self.calls
        return statement_parser.StatementChunk(transactions=[
            statement_parser.StatementRow(date=d(1), description=f"row{n}", amount=10, type="debit", category="food")
        ])


class _FakeLLM:
    def __init__(self, structured):
        self.structured = structured

    def with_structured_output(self, schema):
        return self.structured


async def test_parser_chunks_and_collects_all_rows():
    pages = ["line %d %s" % (i, "x" * 200) for i in range(60)]  # ~ 3 chunks
    fake = _FakeStructured()
    rows = await statement_parser.parse_pages(_FakeLLM(fake), pages)
    assert len(rows) == fake.calls >= 2


async def test_parser_retries_a_failed_chunk_once():
    fake = _FakeStructured(fail_times=1)
    rows = await statement_parser.parse_pages(_FakeLLM(fake), ["one short page"])
    assert len(rows) == 1 and fake.calls == 2


async def test_parser_refuses_partial_results():
    with pytest.raises(StatementError):
        await statement_parser.parse_pages(_FakeLLM(_FakeStructured(fail_times=99)), ["one short page"])
