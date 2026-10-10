"""Purchase orders + the append-only consumption ledger (FR-92, AD-32; Epic 16 A7)."""

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.core.events import subscribe, unsubscribe
from app.events.commercials import PoConsumptionThresholdReached, PoExpiring, PoValidityChanged
from app.models.commercials import PoConsumption
from app.services import commercials_service
from app.services.commercials import po_jobs
from tests.modules.commercials.helpers import (
    API,
    COrg,
    active_contract,
    admin_approves_exceptions,
    consume,
    create_contract,
    create_po,
    err,
    ok,
    po_body,
)


class Events:
    def __init__(self, *types: type) -> None:
        self.types = types
        self.seen: list[object] = []

    def __enter__(self) -> "Events":
        for t in self.types:
            subscribe(t, self.seen.append)
        return self

    def __exit__(self, *exc: object) -> None:
        for t in self.types:
            unsubscribe(t, self.seen.append)


async def test_create_validation(corg: COrg) -> None:
    po = await create_po(corg, po_number="PO-4500")
    assert po["validity"] in ("valid", "upcoming", "expired") and po["remaining_amount"] == "500000.00"
    err(
        await corg.c.post(
            f"{API}/purchase-orders", json=po_body(corg, po_number="po-4500"), headers=corg.h()
        ),
        409,
        "po_number_exists",
    )
    err(
        await corg.c.post(f"{API}/purchase-orders", json=po_body(corg, value_cap=None), headers=corg.h()),
        422,
        "po_cap_required",
    )
    draft = await create_contract(corg)
    err(
        await corg.c.post(
            f"{API}/purchase-orders", json=po_body(corg, contract_id=draft["id"]), headers=corg.h()
        ),
        422,
        "contract_not_active",
    )
    msa = await active_contract(corg)
    linked = await create_po(corg, contract_id=msa["id"])
    assert linked["contract_id"] == msa["id"]
    listed = await ok(await corg.c.get(f"{API}/clients/{corg.client_id}/purchase-orders"))
    assert {p["id"] for p in listed} == {po["id"], linked["id"]}


async def test_acceptance_80_pct_and_insufficient(corg: COrg) -> None:
    po = await create_po(corg, po_number="PO-4500")
    with Events(PoConsumptionThresholdReached) as ev:
        bal = await consume(corg, po["id"], "410000")
        assert bal.remaining_amount == Decimal("90000.00")
        with pytest.raises(ConflictError) as exc:
            await consume(corg, po["id"], "100000")
        assert exc.value.code == "po_insufficient"
    assert [e.pct for e in ev.seen] == [80]  # type: ignore[attr-defined]
    out = await ok(await corg.c.get(f"{API}/purchase-orders/{po['id']}"))
    assert out["remaining_amount"] == "90000.00" and out["pct_used"] == "82.00"
    ledger = await ok(await corg.c.get(f"{API}/purchase-orders/{po['id']}/consumptions"))
    assert [row["amount"] for row in ledger] == ["410000.00"]


async def test_currency_idempotency_credit_note_and_recrossing(corg: COrg) -> None:
    po = await create_po(corg)
    with pytest.raises(ConflictError) as exc:
        await consume(corg, po["id"], "1000", currency="USD")
    assert exc.value.code == "po_currency_mismatch"
    doc = uuid.uuid4()
    with Events(PoConsumptionThresholdReached) as ev:
        await consume(corg, po["id"], "410000", doc=doc)
        again = await consume(corg, po["id"], "410000", doc=doc)  # same document: idempotent
        assert again.consumed_amount == Decimal("410000.00")
        cn = await consume(corg, po["id"], "-50000", kind="credit_note")
        assert cn.consumed_amount == Decimal("360000.00")
        await consume(corg, po["id"], "50000")  # crosses 80 % upwards again
    assert [e.pct for e in ev.seen] == [80, 80]  # type: ignore[attr-defined]


async def test_ledger_is_append_only(corg: COrg) -> None:
    po = await create_po(corg)
    await consume(corg, po["id"], "1000")
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(DBAPIError, match="document_immutable"):
            await db.execute(
                text("UPDATE po_consumptions SET amount = 1 WHERE po_id = :id"), {"id": po["id"]}
            )
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(DBAPIError, match="document_immutable"):
            await db.execute(text("DELETE FROM po_consumptions WHERE po_id = :id"), {"id": po["id"]})


async def test_50_concurrent_consumptions_never_exceed_cap(corg: COrg) -> None:
    po = await create_po(corg, value_cap="100000.00")

    async def one() -> bool:
        try:
            await consume(corg, po["id"], "3000")
        except ConflictError as exc:
            assert exc.code == "po_insufficient"
            return False
        return True

    results = await asyncio.gather(*(one() for _ in range(50)))
    accepted = sum(results)
    assert accepted == 33
    async with tenant_session(corg.tenant_id) as db:
        total = (
            await db.execute(
                select(func.sum(PoConsumption.amount)).where(PoConsumption.po_id == uuid.UUID(po["id"]))
            )
        ).scalar_one()
    assert total == Decimal("3000") * accepted <= Decimal("100000")


async def test_exception_path(corg: COrg) -> None:
    await admin_approves_exceptions(corg)
    po = await create_po(corg, value_cap="1000.00")
    doc = uuid.uuid4()
    with pytest.raises(ConflictError):
        await consume(corg, po["id"], "1500", doc=doc)
    ref = await ok(
        await corg.c.post(
            f"{API}/purchase-orders/{po['id']}/exceptions",
            json={
                "source_doc_kind": "invoice",
                "source_doc_id": str(doc),
                "amount": "1500",
                "reason": "Approved overrun",
            },
            headers=corg.h(),
        ),
        201,
    )
    await ok(
        await corg.c.post(
            f"{API}/approvals/{ref['approval_request_id']}/decide",
            json={"action": "approve"},
            headers=corg.h(),
        )
    )
    bal = await consume(corg, po["id"], "1500", doc=doc)
    assert bal.remaining_amount == Decimal("-500.00")
    ledger = await ok(await corg.c.get(f"{API}/purchase-orders/{po['id']}/consumptions"))
    assert ledger[0]["exception_approval_id"] == ref["approval_request_id"]


async def test_hours_cap_and_validate_po(corg: COrg) -> None:
    po = await create_po(
        corg, value_cap=None, hours_cap="100.00", valid_from="2026-01-01", valid_to="2026-12-31"
    )
    await consume(corg, po["id"], "50000", hours="90")
    ctx = corg.ctx()
    pid = uuid.UUID(po["id"])
    async with tenant_session(corg.tenant_id) as db:
        v = await commercials_service.validate_po(ctx, db, pid, date(2026, 6, 1), hours=Decimal("20"))
        assert (v.valid, v.reason, v.remaining_hours) == (False, "po_insufficient", Decimal("10.00"))
        assert (await commercials_service.validate_po(ctx, db, pid, date(2027, 1, 5))).reason == "po_expired"
        assert (
            await commercials_service.validate_po(ctx, db, pid, date(2026, 6, 1), period_end=date(2027, 2, 1))
        ).reason == "po_expired"
        assert (await commercials_service.validate_po(ctx, db, None, date(2026, 6, 1))).reason == "po_missing"
        ref = await commercials_service.default_po(
            ctx, db, corg.client_id, corg.entity_id, None, date(2026, 6, 1)
        )
        assert ref is not None and ref.po_id == pid
        head = await commercials_service.check_po_headroom(ctx, db, pid, Decimal("0"), Decimal("5"), "INR")
        assert head.valid is True
        balance = await commercials_service.po_balance(ctx, db, pid)
        assert balance.consumed_hours == Decimal("90.00") and balance.remaining_amount is None


async def test_cap_edit_close_cancel(corg: COrg) -> None:
    po = await create_po(corg, value_cap="10000.00")
    await consume(corg, po["id"], "6000")
    err(
        await corg.c.patch(f"{API}/purchase-orders/{po['id']}", json={"value_cap": "5000"}, headers=corg.h()),
        409,
        "po_cap_below_consumed",
    )
    raised = await ok(
        await corg.c.patch(f"{API}/purchase-orders/{po['id']}", json={"value_cap": "20000"}, headers=corg.h())
    )
    assert raised["remaining_amount"] == "14000.00"
    err(
        await corg.c.post(f"{API}/purchase-orders/{po['id']}/cancel", headers=corg.h()),
        409,
        "po_has_consumption",
    )
    with Events(PoValidityChanged) as ev:
        closed = await ok(await corg.c.post(f"{API}/purchase-orders/{po['id']}/close", headers=corg.h()))
        empty = await create_po(corg)
        cancelled = await ok(
            await corg.c.post(f"{API}/purchase-orders/{empty['id']}/cancel", headers=corg.h())
        )
    assert closed["status"] == "closed" and cancelled["status"] == "cancelled"
    assert [e.status for e in ev.seen] == ["closed", "cancelled"]  # type: ignore[attr-defined]
    with pytest.raises(ConflictError) as exc:
        await consume(corg, empty["id"], "1")
    assert exc.value.code == "po_cancelled"
    assert await ok(await corg.c.get(f"{API}/purchase-orders/{po['id']}/placements")) == []


async def test_daily_validity_and_expiry_alerts(corg: COrg) -> None:
    po = await create_po(corg, valid_from="2026-10-01", valid_to="2026-10-20")
    ctx = corg.ctx()
    with Events(PoValidityChanged, PoExpiring) as ev:
        async with tenant_session(corg.tenant_id) as db:
            counts = await po_jobs.run_daily(ctx, db, datetime(2026, 10, 5, 6, tzinfo=UTC))
        async with tenant_session(corg.tenant_id) as db:
            again = await po_jobs.run_daily(ctx, db, datetime(2026, 10, 6, 6, tzinfo=UTC))
        async with tenant_session(corg.tenant_id) as db:
            later = await po_jobs.run_daily(ctx, db, datetime(2026, 10, 21, 6, tzinfo=UTC))
    assert counts == {"expiring": 1, "valid": 1, "expired": 0}
    assert again == {"expiring": 0, "valid": 0, "expired": 0}
    assert later["expired"] == 1
    mine = [e for e in ev.seen if getattr(e, "po_id", None) == uuid.UUID(po["id"])]
    assert [type(e).__name__ for e in mine] == ["PoValidityChanged", "PoExpiring", "PoValidityChanged"]
    listed = await ok(await corg.c.get(f"{API}/purchase-orders", params={"over_pct": 0}))
    assert any(p["id"] == po["id"] for p in listed)
