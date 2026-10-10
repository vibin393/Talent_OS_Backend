# ruff: noqa: E501
"""Epic 30 settlement golden table (R1-R14), concurrency, immutability, period guard and idempotency."""

import asyncio
import contextlib
import random
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.errors import ConflictError, DomainError, ValidationFailed
from app.models.receivables import Allocation, AllocationReversal, ClientCredit, TdsDeduction, WriteOffRequest
from app.schemas.receivables import AllocateIn, AllocationIn, PaymentIn, PaymentReverseIn, ReverseIn, TdsIn
from app.services.receivables import (
    aging,
    credits,
    payments,
    reconciliation,
    settlement,
    statements,
    write_offs,
)
from app.services.receivables import tds as tds_math
from app.services.receivables.common import document
from tests.modules.receivables.conftest import R, ROrg, close_month, err, invoice, ok

D = Decimal
SEP = date(2026, 9, 10)


def pin(org: ROrg, amount: str, allocations: Any = "auto", *, tds: list[TdsIn] | None = None, currency: str = "INR", fx: str | None = None, on: date = SEP) -> PaymentIn:
    return PaymentIn(
        legal_entity_id=org.entity_id, client_id=org.client_id, payment_date=on, amount=D(amount), currency=currency,
        fx_rate=D(fx) if fx else None, mode="neft", reference=f"UTR{uuid.uuid4().hex[:8]}", allocations=allocations, tds=tds or [],
    )  # fmt: skip


def alloc(doc_id: uuid.UUID, amount: str) -> AllocationIn:
    return AllocationIn(document_kind="invoice", document_id=doc_id, amount=D(amount))


async def record(org: ROrg, data: PaymentIn) -> Any:
    async with org.session() as db:
        return await payments.record(org.ctx(), db, data)


async def open_of(org: ROrg, doc_id: uuid.UUID, as_of: date | None = None) -> settlement.Balance:
    async with org.session() as db:
        doc = await document(db, "invoice", doc_id)
        return (await settlement.balances(db, [doc], as_of))[doc_id]


# ---------------------------------------------------------------- pure TDS
def test_tds_pure() -> None:
    assert tds_math.expected_tds(D("100000.00"), D("2")) == D("2000.00")
    assert tds_math.expected_tds(D("50000.00"), D("2")) == D("1000.00")  # R3: RCM base = taxable 50000
    assert tds_math.variance(D("2000.00"), D("1800.00"), D("1.00")) is True  # R4
    assert tds_math.variance(D("2000.00"), D("1999.50"), D("1.00")) is False


# ---------------------------------------------------------------- golden table
async def test_r1_r4_tds_and_cash(rorg: ROrg) -> None:
    inv = await invoice(rorg, "118000.00", taxable="100000.00")
    out = await record(rorg, pin(rorg, "116000.00", [alloc(inv, "116000.00")], tds=[TdsIn(document_kind="invoice", document_id=inv, rate_pct=D("2"), amount=D("2000.00"))]))
    assert out.tds[0].expected_amount == D("2000.00") and out.tds[0].variance_flag is False
    b = await open_of(rorg, inv)
    assert b.open == D("0.00") and b.status == "paid"
    # R2: GST-TDS on a government client
    inv2 = await invoice(rorg, "118000.00", taxable="100000.00")
    await record(rorg, pin(rorg, "114000.00", [alloc(inv2, "114000.00")], tds=[
        TdsIn(document_kind="invoice", document_id=inv2, rate_pct=D("2"), amount=D("2000.00")),
        TdsIn(document_kind="invoice", document_id=inv2, kind="gst_tds", rate_pct=D("2"), amount=D("2000.00")),
    ]))  # fmt: skip
    assert (await open_of(rorg, inv2)).status == "paid"
    # R3 / R4: RCM base and variance flag
    inv3 = await invoice(rorg, "50000.00", taxable="50000.00")
    out3 = await record(rorg, pin(rorg, "1000.00", [alloc(inv3, "1000.00")], tds=[TdsIn(document_kind="invoice", document_id=inv3, rate_pct=D("2"), amount=D("800.00"))]))
    assert out3.tds[0].base_amount == D("50000.00") and out3.tds[0].expected_amount == D("1000.00") and out3.tds[0].variance_flag is True


async def test_r5_over_allocation_and_r14_currency(rorg: ROrg) -> None:
    inv = await invoice(rorg, "1000.00")
    body = pin(rorg, "2000.00", [alloc(inv, "1000.01")]).model_dump(mode="json")
    err(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(str(uuid.uuid4()))), 409, "allocation_exceeds_open")
    body = pin(rorg, "500.00", [alloc(inv, "500.00")], currency="USD", fx="84").model_dump(mode="json")
    err(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(str(uuid.uuid4()))), 422, "currency_mismatch")
    body = pin(rorg, "500.00", [alloc(inv, "400.00"), alloc(inv, "100.00")]).model_dump(mode="json")
    body["amount"] = "450.00"
    err(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(str(uuid.uuid4()))), 422, "over_allocated")


async def test_r6_fx_full_settle(rorg: ROrg) -> None:
    inv = await invoice(rorg, "786.66", currency="USD", fx="83.123456", inr_total="65389.80")
    out = await record(rorg, pin(rorg, "786.66", [alloc(inv, "786.66")], currency="USD", fx="84.000000"))
    a = out.allocations[0]
    assert (a.inr_at_payment, a.inr_at_document, a.realized_fx) == (D("66079.44"), D("65389.80"), D("689.64"))


async def test_r7_r8_fx_partial_and_reversal(rorg: ROrg) -> None:
    inv = await invoice(rorg, "786.66", currency="USD", fx="83.123456", inr_total="65389.80")
    out = await record(rorg, pin(rorg, "786.66", [alloc(inv, "400.00")], currency="USD", fx="84.000000"))
    assert out.unallocated == D("386.66")
    async with rorg.session() as db:
        out = await payments.allocate(rorg.ctx(), db, out.id, AllocateIn(allocations=[alloc(inv, "386.66")], allocation_date=SEP))
    rows = sorted(out.allocations, key=lambda a: -a.amount)
    assert (rows[0].inr_at_document, rows[0].inr_at_payment, rows[0].realized_fx) == (D("33249.38"), D("33600.00"), D("350.62"))
    assert (rows[1].inr_at_document, rows[1].inr_at_payment, rows[1].realized_fx) == (D("32140.42"), D("32479.44"), D("339.02"))
    assert rows[0].realized_fx + rows[1].realized_fx == D("689.64")
    assert out.allocation_status == "fully_allocated"
    async with rorg.session() as db:
        await payments.reverse_allocation(rorg.ctx(), db, rows[0].id, ReverseIn(reason="wrong document", reversal_date=SEP))
    async with rorg.session() as db:
        rev = (await db.execute(select(AllocationReversal).where(AllocationReversal.allocation_id == rows[0].id))).scalar_one()
    assert (rev.amount, rev.inr_at_document, rev.inr_at_payment, rev.realized_fx) == (D("-400.00"), D("-33249.38"), D("-33600.00"), D("-350.62"))
    assert (await open_of(rorg, inv)).open == D("400.00")
    async with rorg.session() as db:
        with pytest.raises(ConflictError) as exc:
            await payments.reverse_allocation(rorg.ctx(), db, rows[0].id, ReverseIn(reason="again"))
    assert exc.value.code == "already_reversed"


async def test_r9_remainder_credit_and_reversal(rorg: ROrg) -> None:
    inv = await invoice(rorg, "118000.00")
    out = await record(rorg, pin(rorg, "150000.00"))
    assert [a.amount for a in out.allocations] == [D("118000.00")]
    assert out.unallocated == D("32000.00")
    async with rorg.session() as db:
        assert await credits.balance(db, rorg.client_id) == D("32000.00")
    # bounced cheque: reversal re-opens the invoice and clears the credit
    async with rorg.session() as db:
        rev = await payments.reverse(rorg.ctx(), db, out.id, PaymentReverseIn(reason="cheque bounced", reversal_date=date(2026, 9, 20)))
    assert rev.allocation_status == "reversed" and rev.unallocated == D("0.00")
    assert (await open_of(rorg, inv)).status == "unpaid"
    async with rorg.session() as db:
        assert await credits.balance(db, rorg.client_id) == D("0.00")
        with pytest.raises(ConflictError):
            await payments.reverse(rorg.ctx(), db, out.id, PaymentReverseIn(reason="twice"))


async def test_r10_credit_note_excess_idempotent(rorg: ROrg) -> None:
    inv = await invoice(rorg, "50000.00")
    await record(rorg, pin(rorg, "50000.00", [alloc(inv, "50000.00")]))
    cn = await invoice(rorg, "10000.00", kind="credit_note", original_invoice_id=inv, doc_date=date(2026, 9, 15))
    async with rorg.session() as db:
        assert await credits.apply_credit_note(rorg.ctx(), db, cn) == "applied"
    async with rorg.session() as db:
        assert await credits.apply_credit_note(rorg.ctx(), db, cn) == "duplicate"
        rows = list((await db.execute(select(ClientCredit).where(ClientCredit.credit_note_id == cn))).scalars())
    assert [(r.kind, r.amount) for r in rows] == [("credit_note_excess", D("10000.00"))]
    # partially open invoice: the CN is allocated to it
    inv2 = await invoice(rorg, "30000.00")
    cn2 = await invoice(rorg, "5000.00", kind="credit_note", original_invoice_id=inv2)
    async with rorg.session() as db:
        await credits.apply_credit_note(rorg.ctx(), db, cn2)
    assert (await open_of(rorg, inv2)).open == D("25000.00")


async def test_r11_write_off_race_lapses(rorg: ROrg) -> None:
    inv = await invoice(rorg, "500.00")
    async with rorg.session() as db:
        req = WriteOffRequest(id=uuid.uuid4(), tenant_id=rorg.tenant_id, legal_entity_id=rorg.entity_id, client_id=rorg.client_id, invoice_id=inv, currency="INR", requested_amount=D("500.00"), reason="small balance", status="approved")
        db.add(req)
    await record(rorg, pin(rorg, "200.00", [alloc(inv, "200.00")]))
    async with rorg.session() as db:
        out = await write_offs.execute(rorg.ctx(), db, req.id, SEP)
    assert out.status == "lapsed" and out.status_reason == "balance changed"
    assert (await open_of(rorg, inv)).open == D("300.00")
    # the normal path (no approval chain configured -> auto approved -> executed in the callback)
    resp = await rorg.c.post(f"{R}/write-offs", json={"document_kind": "invoice", "document_id": str(inv), "amount": "300.00", "reason": "uncollectible"}, headers=rorg.h(str(uuid.uuid4())))
    body = await ok(resp, 201)
    assert body["status"] == "executed"
    assert (await open_of(rorg, inv)).status == "written_off"


async def test_r12_aging_as_of(rorg: ROrg) -> None:
    inv = await invoice(rorg, "1000.00", doc_date=date(2025, 12, 20), due=date(2026, 1, 1))
    out = await record(rorg, pin(rorg, "1000.00", [alloc(inv, "1000.00")], on=date(2026, 1, 10)))
    async with rorg.session() as db:
        await payments.reverse_allocation(rorg.ctx(), db, out.allocations[0].id, ReverseIn(reason="misapplied", reversal_date=date(2026, 1, 20)))
    async with rorg.session() as db:
        mid = await aging.query(rorg.ctx(), db, rorg.entity_id, date(2026, 1, 15), "document")
        late = await aging.query(rorg.ctx(), db, rorg.entity_id, date(2026, 1, 25), "document")
        before = await aging.query(rorg.ctx(), db, rorg.entity_id, date(2025, 12, 31), "document")
    assert not [r for r in mid.rows if r.document_id == inv]
    row = next(r for r in late.rows if r.document_id == inv)
    assert row.buckets["1-30"] == D("1000.00") and row.total_open_inr == D("1000.00")
    assert next(r for r in before.rows if r.document_id == inv).buckets["not_due"] == D("1000.00")


async def test_r13_statement_and_reconciliation(rorg: ROrg) -> None:
    inv1 = await invoice(rorg, "10000.00", doc_date=date(2026, 8, 5), due=date(2026, 9, 4))
    inv2 = await invoice(rorg, "20000.00", doc_date=date(2026, 9, 2))
    await record(rorg, pin(rorg, "12000.00", on=date(2026, 9, 5)))
    p2 = await record(rorg, pin(rorg, "5000.00", [alloc(inv2, "5000.00")], on=date(2026, 9, 6)))
    async with rorg.session() as db:
        await payments.reverse(rorg.ctx(), db, p2.id, PaymentReverseIn(reason="bounced", reversal_date=date(2026, 9, 8)))
    async with rorg.session() as db:
        st = await statements.build(rorg.ctx(), db, rorg.client_id, "INR", date(2026, 9, 1), date(2026, 9, 30))
        docs = [await document(db, "invoice", i) for i in (inv1, inv2)]
        bals = await settlement.balances(db, docs)
        credit = await credits.balance(db, rorg.client_id, currency="INR")
    assert st.opening_balance == D("10000.00")
    assert st.closing_balance == sum((b.open for b in bals.values()), D(0)) - credit == D("18000.00")
    async with rorg.session() as db:
        await settlement.rebuild_cache(db)
    async with rorg.session() as db:
        found = []
        for fn in reconciliation.CHECKS.values():
            found += await fn(rorg.ctx(), db, date(2026, 9, 30))
    assert found == []


# ---------------------------------------------------------------- concurrency
async def test_parallel_full_allocations_one_wins(rorg: ROrg) -> None:
    inv = await invoice(rorg, "7000.00")

    async def attempt() -> str:
        try:
            await record(rorg, pin(rorg, "7000.00", [alloc(inv, "7000.00")]))
        except DomainError as exc:
            return exc.code
        return "ok"

    results = sorted(await asyncio.gather(attempt(), attempt()))
    assert results == ["allocation_exceeds_open", "ok"]
    assert (await open_of(rorg, inv)).open == D("0.00")


async def test_parallel_write_off_and_allocation_never_negative(rorg: ROrg) -> None:
    inv = await invoice(rorg, "900.00")
    async with rorg.session() as db:
        req = WriteOffRequest(id=uuid.uuid4(), tenant_id=rorg.tenant_id, legal_entity_id=rorg.entity_id, client_id=rorg.client_id, invoice_id=inv, currency="INR", requested_amount=D("900.00"), reason="x", status="approved")
        db.add(req)

    async def wo() -> None:
        async with rorg.session() as db:
            await write_offs.execute(rorg.ctx(), db, req.id, SEP)

    async def pay() -> None:
        with contextlib.suppress(DomainError):
            await record(rorg, pin(rorg, "900.00", [alloc(inv, "900.00")]))

    await asyncio.gather(wo(), pay())
    assert (await open_of(rorg, inv)).open == D("0.00")


# ---------------------------------------------------------------- immutability / period guard
async def test_facts_immutable_and_period_guard(rorg: ROrg) -> None:
    inv = await invoice(rorg, "1000.00")
    out = await record(rorg, pin(rorg, "1000.00", [alloc(inv, "1000.00")], tds=[]))
    for sql in (
        "UPDATE payments SET amount = 1 WHERE id = :i",
        "UPDATE payments SET payment_date = '2026-09-11' WHERE id = :i",
        "DELETE FROM payments WHERE id = :i",
        "UPDATE allocations SET amount = 1 WHERE payment_id = :i",
        "DELETE FROM allocations WHERE payment_id = :i",
        "TRUNCATE allocations",
    ):
        with pytest.raises(DBAPIError):
            async with rorg.session() as db:
                await db.execute(text(sql), {"i": out.id})
    # allow-listed markers pass
    async with rorg.session() as db:
        await db.execute(text("UPDATE payments SET reversal_reason = reversal_reason WHERE id = :i"), {"i": out.id})
    # TDS certificate fields are allow-listed
    inv2 = await invoice(rorg, "11800.00", taxable="10000.00")
    p2 = await record(rorg, pin(rorg, "11600.00", [alloc(inv2, "11600.00")], tds=[TdsIn(document_kind="invoice", document_id=inv2, rate_pct=D("2"), amount=D("200.00"))]))
    resp = await rorg.c.patch(f"{R}/tds-deductions/{p2.tds[0].id}/certificate", json={"certificate_status": "received", "certificate_ref": "16A-1", "certificate_received_at": "2026-10-01"}, headers=rorg.h())
    assert (await ok(resp))["certificate_status"] == "received"
    with pytest.raises(DBAPIError):
        async with rorg.session() as db:
            await db.execute(text("UPDATE tds_deductions SET amount = 1 WHERE id = :i"), {"i": p2.tds[0].id})
    # closed month: service and DB both refuse
    await close_month(rorg, 2026, 8)
    inv3 = await invoice(rorg, "100.00")
    with pytest.raises(ConflictError) as exc:
        await record(rorg, pin(rorg, "100.00", [alloc(inv3, "100.00")], on=date(2026, 8, 20)))
    assert exc.value.code == "period_closed" and exc.value.details["period"] == "2026-08"
    with pytest.raises(DBAPIError):
        async with rorg.session() as db:
            await db.execute(
                text("INSERT INTO client_credits (id, tenant_id, legal_entity_id, client_id, currency, credit_date, kind, amount) VALUES (:id, :t, :le, :c, 'INR', '2026-08-15', 'payment_remainder', 5)"),
                {"id": uuid.uuid4(), "t": rorg.tenant_id, "le": rorg.entity_id, "c": rorg.client_id},
            )
    # a missing period row also refuses
    with pytest.raises(ConflictError):
        await record(rorg, pin(rorg, "100.00", [], on=date(2031, 1, 15)))
    # late reversal of an allocation dated in the closed month -> today, prior_period
    _ = ValidationFailed


# ---------------------------------------------------------------- idempotency / tenancy
async def test_payment_idempotency_and_tenancy(rorg: ROrg, other_rorg: ROrg) -> None:
    inv = await invoice(rorg, "3000.00")
    body = pin(rorg, "3000.00", [alloc(inv, "3000.00")]).model_dump(mode="json")
    key = str(uuid.uuid4())
    first = await ok(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(key)), 201)
    again = await ok(await rorg.c.post(f"{R}/payments", json=body, headers=rorg.h(key)), 201)
    assert first["id"] == again["id"]
    body2 = {**body, "amount": "2999.00"}
    resp = await rorg.c.post(f"{R}/payments", json=body2, headers=rorg.h(key))
    assert resp.status_code in (409, 422), resp.text
    listed = await ok(await rorg.c.get(f"{R}/payments", params={"client_id": str(rorg.client_id)}))
    assert len(listed) == 1
    # tenant B cannot see tenant A's payment, allocations or document
    assert (await other_rorg.c.get(f"{R}/payments/{first['id']}")).status_code == 404
    assert (await other_rorg.c.post(f"{R}/allocations/{first['allocations'][0]['id']}/reverse", json={"reason": "x"}, headers=other_rorg.h(str(uuid.uuid4())))).status_code == 404
    other = pin(other_rorg, "10.00", [alloc(inv, "10.00")]).model_dump(mode="json")
    assert (await other_rorg.c.post(f"{R}/payments", json=other, headers=other_rorg.h(str(uuid.uuid4())))).status_code == 404


# ---------------------------------------------------------------- property (seeded random sequences)
@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_random_sequences_match_rebuild(rorg: ROrg, seed: int) -> None:
    rng = random.Random(seed)
    docs = [await invoice(rorg, f"{rng.randint(100, 5000)}.{rng.randint(0, 99):02d}") for _ in range(3)]
    for _ in range(8):
        op = rng.choice(["pay", "pay", "reverse_alloc", "tds", "reverse_payment"])
        try:
            if op == "pay":
                await record(rorg, pin(rorg, f"{rng.randint(50, 3000)}.00"))
            elif op == "tds":
                d = rng.choice(docs)
                await record(rorg, pin(rorg, "1.00", [], tds=[TdsIn(document_kind="invoice", document_id=d, rate_pct=D("2"), amount=D("10.00"))]))
            else:
                async with rorg.session() as db:
                    live = list((await db.execute(select(Allocation).where(Allocation.id.not_in(select(AllocationReversal.allocation_id))))).scalars())
                if not live:
                    continue
                pick = rng.choice(live)
                async with rorg.session() as db:
                    if op == "reverse_alloc":
                        await payments.reverse_allocation(rorg.ctx(), db, pick.id, ReverseIn(reason="r"))
                    elif pick.payment_id is not None:
                        await payments.reverse(rorg.ctx(), db, pick.payment_id, PaymentReverseIn(reason="r"))
        except DomainError:
            continue
    async with rorg.session() as db:
        for d in docs:
            assert (await settlement.open_balance(db, await document(db, "invoice", d))) >= 0
        assert await reconciliation.check_payments(rorg.ctx(), db, date(2026, 12, 31)) == []
        assert await reconciliation.check_documents(rorg.ctx(), db, date(2026, 12, 31)) == []
        tds_total = (await db.execute(select(TdsDeduction))).scalars().all()
        assert all(t.amount > 0 for t in tds_total)
