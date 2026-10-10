"""AD-31 generic SLA clocks over the P1 ``sla_targets`` table (Epic 13, story 13.7 / R14)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.business_hours import Calendar
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.sla import SlaTarget
from app.services import sla_service
from app.services.sla.registry import _milestones
from tests.modules.identity.conftest import make_tenant

SUBJECT = "p13_invoice"


@pytest.fixture(autouse=True)
def _milestone() -> Iterator[None]:
    with _milestones.override(
        (SUBJECT, "payment"), sla_service.SlaMilestoneDef("Payment", scope_keys=("client_id",))
    ):
        yield


async def _target(
    tenant_id: uuid.UUID, hours: str, scope: dict[str, str] | None = None, *, calendar: str = "calendar"
) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        row = SlaTarget(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            subject_type=SUBJECT,
            milestone="payment",
            scope=scope or {},
            target_hours=Decimal(hours),
            clock=calendar,
            calendar=calendar,
            at_risk_percent=50,
            is_active=True,
        )
        db.add(row)
        return row.id


async def test_unregistered_milestone_and_no_target() -> None:
    tenant_id = (await make_tenant()).id
    ctx = RequestContext(tenant_id=tenant_id)
    async with tenant_session(tenant_id) as db:
        with pytest.raises(ValueError, match="unregistered"):
            await sla_service.start_clock(ctx, db, SUBJECT, uuid.uuid4(), "nope")
        assert await sla_service.start_clock(ctx, db, SUBJECT, uuid.uuid4(), "payment") is None


async def test_start_is_idempotent_most_specific_target_wins_and_stop() -> None:
    tenant_id = (await make_tenant()).id
    ctx = RequestContext(tenant_id=tenant_id)
    client_id = uuid.uuid4()
    await _target(tenant_id, "48")
    specific = await _target(tenant_id, "10", {"client_id": str(client_id)})
    start = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    subject = uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        clock = await sla_service.start_clock(
            ctx,
            db,
            SUBJECT,
            subject,
            "payment",
            started_at=start,
            scope={"client_id": client_id, "priority": None},
        )
        assert clock is not None and clock.target_id == specific
        assert clock.due_at == start + timedelta(hours=10)
        assert clock.at_risk_at == start + timedelta(hours=5)
        again = await sla_service.start_clock(ctx, db, SUBJECT, subject, "payment")
        assert again is not None and again.id == clock.id
        # another client falls back to the unscoped target
        other = await sla_service.start_clock(
            ctx, db, SUBJECT, uuid.uuid4(), "payment", started_at=start, scope={"client_id": uuid.uuid4()}
        )
        assert other is not None and other.due_at == start + timedelta(hours=48)

        cal = Calendar({})
        assert sla_service.compute_status(clock, start + timedelta(hours=1), cal, "Asia/Kolkata") == "healthy"
        assert sla_service.compute_status(clock, start + timedelta(hours=6), cal, "Asia/Kolkata") == "at_risk"
        assert (
            sla_service.compute_status(clock, start + timedelta(hours=11), cal, "Asia/Kolkata") == "breached"
        )

        await sla_service.stop_clock(ctx, db, SUBJECT, subject, "payment", at=start + timedelta(hours=3))
        clocks = await sla_service.clocks_for(ctx, db, SUBJECT, [subject])
        [done] = clocks[subject]
        assert done.outcome == "met"
        assert sla_service.compute_status(done, start + timedelta(hours=30), cal, "Asia/Kolkata") == "met"
        # a new clock can start once the previous one completed
        restarted = await sla_service.start_clock(
            ctx, db, SUBJECT, subject, "payment", scope={"client_id": client_id}
        )
        assert restarted is not None and restarted.id != clock.id
        await sla_service.cancel_clock(ctx, db, SUBJECT, subject, "payment")
        clocks = await sla_service.clocks_for(ctx, db, SUBJECT, [subject])
        assert [c.outcome for c in clocks[subject]] == ["met", "cancelled"]


async def test_late_stop_is_breached_and_ref_ids_are_separate() -> None:
    tenant_id = (await make_tenant()).id
    ctx = RequestContext(tenant_id=tenant_id)
    await _target(tenant_id, "2")
    start = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    subject, r1, r2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        a = await sla_service.start_clock(ctx, db, SUBJECT, subject, "payment", ref_id=r1, started_at=start)
        b = await sla_service.start_clock(ctx, db, SUBJECT, subject, "payment", ref_id=r2, started_at=start)
        assert a is not None and b is not None and a.id != b.id
        await sla_service.stop_clock(
            ctx, db, SUBJECT, subject, "payment", ref_id=r1, at=start + timedelta(hours=5)
        )
        clocks = (await sla_service.clocks_for(ctx, db, SUBJECT, [subject]))[subject]
        assert {c.ref_id: c.outcome for c in clocks} == {r1: "breached", r2: None}


async def test_p1_demand_targets_untouched() -> None:
    tenant_id = (await make_tenant()).id
    async with tenant_session(tenant_id) as db:
        rows = (await db.execute(select(SlaTarget).where(SlaTarget.subject_type != "demand"))).scalars().all()
        assert rows == []
