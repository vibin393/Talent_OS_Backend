# ruff: noqa: E501
"""Gap-free numbering under concurrency, rollback reuse, FY boundary, database immutability and note caps / deadline
(spec A14: numbering, immutability, G19, G20). Runs as the ``talent_app`` role through ``tenant_session``."""

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import fiscal
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.billing import Invoice, InvoiceLine
from app.schemas.billing import CreditNoteIn, ManualInvoiceIn, ManualLineIn, NoteLineIn
from app.services import legal_entities_service
from app.services.billing import integrity, issue, notes
from app.services.billing import invoices as inv_service
from tests.modules.billing.conftest import BOrg

D = Decimal


async def _drafts(org: BOrg, n: int, amount: str = "1000.00") -> list[uuid.UUID]:
    ids = []
    async with tenant_session(org.tenant_id) as db:
        for _ in range(n):
            out = await inv_service.create_manual(
                org.ctx(),
                db,
                ManualInvoiceIn(
                    client_id=org.client_id,
                    legal_entity_id=org.entity_id,
                    lines=[ManualLineIn(description="Fee", rate=D(amount))],
                ),
            )
            ids.append(out.id)
    return ids


async def _issue(org: BOrg, inv_id: uuid.UUID, doc_date: date | None = None) -> str:
    async with tenant_session(org.tenant_id) as db:
        return str((await issue.issue_invoice(org.ctx(), db, inv_id, doc_date)).number)


def _seq(number: str) -> int:
    return int(number.rsplit("/", 1)[1])


async def test_fifty_parallel_issues_are_gap_free(borg: BOrg) -> None:
    ids = await _drafts(borg, 50)
    # 50 issues race on the series lock, but at most 8 hold a pooled connection at once so the app pool
    # (pool_size + max_overflow) is never exhausted under machine load; gap-free/exactly-once is unchanged.
    sem = asyncio.Semaphore(8)

    async def bounded(inv_id: uuid.UUID) -> str:
        async with sem:
            return await _issue(borg, inv_id)

    numbers = await asyncio.wait_for(asyncio.gather(*(bounded(i) for i in ids)), timeout=600)
    assert len(set(numbers)) == 50
    assert sorted(_seq(n) for n in numbers) == list(range(1, 51))
    async with tenant_session(borg.tenant_id) as db:
        assert await integrity.check_numbering(borg.ctx(), db, date.today()) == []


async def test_fault_after_assign_rolls_back_and_number_is_reused(
    borg: BOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = await _drafts(borg, 2)

    async def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("fault after assign")

    monkeypatch.setattr(issue, "content_hash", boom)
    with pytest.raises(RuntimeError):
        await _issue(borg, first)
    monkeypatch.undo()
    assert await _issue(borg, second) == "INV/26-27/0001"
    assert await _issue(borg, first) == "INV/26-27/0002"


async def test_fiscal_year_boundary_starts_new_counter(borg: BOrg) -> None:
    from datetime import UTC, datetime

    local = fiscal.local_date(datetime(2027, 3, 31, 19, 0, tzinfo=UTC), "Asia/Kolkata")
    assert local == date(2027, 4, 1) and fiscal.fiscal_year(local) == "27-28"
    async with tenant_session(borg.tenant_id) as db:
        series = await legal_entities_service.series_for(
            borg.ctx(),
            db,
            legal_entity_id=borg.entity_id,
            gst_registration_id=borg.registration_id,
            doc_type="invoice",
        )
        assert await legal_entities_service.assign(db, series, local) == "INV/27-28/0001"
        await db.rollback()


async def test_issued_invoice_is_immutable_at_the_database(borg: BOrg) -> None:
    (inv_id,) = await _drafts(borg, 1)
    await _issue(borg, inv_id)
    tid = borg.tenant_id
    for stmt in (
        "UPDATE invoices SET taxable_total = 1 WHERE id = :i",
        "UPDATE invoice_lines SET taxable_value = 1 WHERE invoice_id = :i",
        "UPDATE invoice_tax_lines SET amount = 1 WHERE invoice_id = :i",
        "DELETE FROM invoices WHERE id = :i",
        "UPDATE invoices SET status = 'cancelled' WHERE id = :i",
    ):
        with pytest.raises(DBAPIError):
            async with tenant_session(tid) as db:
                await db.execute(text(stmt), {"i": inv_id})
    with pytest.raises(DBAPIError):
        async with tenant_session(tid) as db:
            line = (
                (await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == inv_id)))
                .scalars()
                .first()
            )
            assert line is not None
            await db.execute(
                text(
                    "INSERT INTO invoice_lines (id, tenant_id, invoice_id, line_no, line_kind, description, sac_code, quantity, rate, effective_rate, gross_amount, taxable_value) "
                    "VALUES (:id, :t, :i, 99, 'manual', 'x', '998513', 1, 1, 1, 1, 1)"
                ),
                {"id": uuid.uuid4(), "t": tid, "i": inv_id},
            )
    with pytest.raises(DBAPIError):
        async with tenant_session(tid) as db:
            await db.execute(text("TRUNCATE invoice_tax_lines"))
    async with tenant_session(tid) as db:
        before = (await db.execute(select(Invoice.content_hash).where(Invoice.id == inv_id))).scalar()
        await db.execute(
            text("UPDATE invoices SET irn = :irn, ack_no = '1', delivery_status = 'sent' WHERE id = :i"),
            {"irn": "a" * 64, "i": inv_id},
        )
    async with tenant_session(tid) as db:
        row = (await db.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert row.content_hash == before and await issue.content_hash(db, "invoice", row) == before
    # the owner role can bypass the trigger; the integrity job notices
    engine = create_async_engine(get_settings().owner_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE invoices DISABLE TRIGGER tos_immutable"))
            await conn.execute(text("UPDATE invoices SET notes = 'tampered' WHERE id = :i"), {"i": inv_id})
            await conn.execute(text("ALTER TABLE invoices ENABLE TRIGGER tos_immutable"))
    finally:
        await engine.dispose()
    async with tenant_session(tid) as db:
        findings = await integrity.check_content_hash(borg.ctx(), db, date.today())
        assert [f.subject_id for f in findings] == [inv_id]


async def test_g19_credit_note_remainder_and_cap(borg: BOrg) -> None:
    (inv_id,) = await _drafts(borg, 1, "10.00")
    await _issue(borg, inv_id)
    async with tenant_session(borg.tenant_id) as db:
        line_id = (
            await db.execute(select(InvoiceLine.id).where(InvoiceLine.invoice_id == inv_id))
        ).scalar_one()
    cgst: list[Decimal] = []
    from app.core.errors import ValidationFailed

    for i, amount in enumerate(["1.50"] * 6 + ["1.00"]):
        if i == 6:  # 1.00 remains: 1.01 exceeds the cap
            with pytest.raises(ValidationFailed) as exc:
                async with tenant_session(borg.tenant_id) as db:
                    await notes.create_credit_note(
                        borg.ctx(),
                        db,
                        inv_id,
                        CreditNoteIn(
                            reason="discount",
                            lines=[NoteLineIn(original_invoice_line_id=line_id, amount=D("1.01"))],
                        ),
                    )
            assert exc.value.code == "credit_exceeds_original"
        async with tenant_session(borg.tenant_id) as db:
            cn = await notes.create_credit_note(
                borg.ctx(),
                db,
                inv_id,
                CreditNoteIn(
                    reason="discount", lines=[NoteLineIn(original_invoice_line_id=line_id, amount=D(amount))]
                ),
            )
            out = await notes.issue_note(borg.ctx(), db, "credit_note", cn.id)
            cgst.append(next(t.amount for t in out.lines[0].taxes if t.component == "cgst"))
    assert cgst == [D("0.14")] * 6 + [D("0.06")]
    # the credit that exhausts every line is a full credit: the invoice is cancelled, nothing more can be credited
    async with tenant_session(borg.tenant_id) as db:
        inv = (await db.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert (inv.status, inv.cancel_basis) == ("cancelled", "full_credit_note")


async def test_g20_credit_note_gst_deadline(borg: BOrg) -> None:
    async with tenant_session(borg.tenant_id) as db:
        await legal_entities_service.ensure_periods(db, borg.entity_id, date(2026, 3, 1), date(2026, 3, 31))
    (inv_id,) = await _drafts(borg, 1)
    assert (await _issue(borg, inv_id, date(2026, 3, 15))).startswith("INV/25-26/")
    async with tenant_session(borg.tenant_id) as db:
        line_id = (
            await db.execute(select(InvoiceLine.id).where(InvoiceLine.invoice_id == inv_id))
        ).scalar_one()
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError) as exc:
        async with tenant_session(borg.tenant_id) as db:
            cn = await notes.create_credit_note(
                borg.ctx(),
                db,
                inv_id,
                CreditNoteIn(
                    reason="discount",
                    lines=[NoteLineIn(original_invoice_line_id=line_id, amount=D("100.00"))],
                ),
            )
            await notes.issue_note(borg.ctx(), db, "credit_note", cn.id, date(2026, 12, 1))
    assert exc.value.code == "cn_gst_deadline_passed"
    async with tenant_session(borg.tenant_id) as db:
        cn = await notes.create_credit_note(
            borg.ctx(), db, inv_id, CreditNoteIn(reason="commercial", gst_adjusted=False, amount=D("100.00"))
        )
        out = await notes.issue_note(borg.ctx(), db, "credit_note", cn.id, date(2026, 12, 1))
    assert out.number == "CN/26-27/0001" and out.charged_tax_total == 0 and out.lines[0].taxes == []
