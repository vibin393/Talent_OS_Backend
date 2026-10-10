# ruff: noqa: E501
"""Epic 32 story 19.1 (FR-168): generic SLA targets, clocks, evaluator, notifications, org RLS, endpoints."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import OrgScope, tenant_session
from app.core.events import subscribe
from app.events.sla import SlaClockStatusChanged
from app.models.sla import SlaClock
from app.services import sla_service
from app.services.sla import evaluator
from app.services.sla.registry import SlaMilestoneDef
from tests.epic10 import DOrg, h, member
from tests.sla.helpers import SLA, milestone, scalar, set_work_week

VR = ("vendor_release", "first_submission")


@pytest.fixture
def recipients_box() -> dict[str, Any]:
    return {"users": set()}


@pytest.fixture
def vendor_milestone(recipients_box: dict[str, Any]) -> Any:
    async def recipients(db: Any, subject_id: uuid.UUID) -> set[uuid.UUID]:
        return set(recipients_box["users"])

    definition = SlaMilestoneDef(
        "First vendor submission",
        scope_keys=("vendor_id",),
        recipients=recipients,
        link=lambda sid: f"/vendors/releases/{sid}",
    )
    with milestone(*VR, definition):
        yield definition


async def _post_target(dorg: DOrg, **body: Any) -> dict[str, Any]:
    payload = {"subject_type": VR[0], "milestone": VR[1], "target_hours": "48", **body}
    resp = await dorg.admin.client.post(f"{SLA}/targets", json=payload, headers=h(dorg.admin))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_milestones_catalogue_and_availability(dorg: DOrg, vendor_milestone: Any) -> None:
    resp = await dorg.admin.client.get(f"{SLA}/milestones")
    assert resp.status_code == 200, resp.text
    items = {(m["subject_type"], m["milestone"]): m for m in resp.json()}
    assert items[VR]["available"] is True and items[VR]["scope_keys"] == ["vendor_id"]
    assert items["dispute", "resolution"]["owner_epic"] == "30"
    for key in [
        ("bgv", "check_turnaround"),
        ("timesheet", "step_decision"),
        ("invoice", "issue_after_period"),
    ]:
        assert key in items


async def test_target_validation_and_demand_refused(dorg: DOrg, vendor_milestone: Any) -> None:
    c = dorg.admin.client
    resp = await c.post(
        f"{SLA}/targets",
        json={"subject_type": "demand", "milestone": "assignment", "target_hours": "4"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "use_demand_sla_settings"
    resp = await c.post(
        f"{SLA}/targets",
        json={"subject_type": VR[0], "milestone": VR[1], "target_hours": "0"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    resp = await c.post(
        f"{SLA}/targets",
        json={"subject_type": VR[0], "milestone": VR[1], "target_hours": "9000"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    resp = await c.post(
        f"{SLA}/targets",
        json={"subject_type": VR[0], "milestone": VR[1], "target_hours": "4", "at_risk_percent": 100},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422
    resp = await c.post(
        f"{SLA}/targets",
        json={
            "subject_type": VR[0],
            "milestone": VR[1],
            "target_hours": "4",
            "scope": {"bgv_package_id": str(uuid.uuid4())},
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["details"]["reason"] == "scope_mismatch"
    resp = await c.post(
        f"{SLA}/targets",
        json={"subject_type": "nope", "milestone": "x", "target_hours": "4"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["details"]["reason"] == "unknown_milestone"
    created = await _post_target(dorg)
    dup = await c.post(
        f"{SLA}/targets",
        json={"subject_type": VR[0], "milestone": VR[1], "target_hours": "8"},
        headers=h(dorg.admin),
    )
    assert dup.status_code == 409
    upd = await c.patch(
        f"{SLA}/targets/{created['id']}",
        json={"target_hours": "12", "priority": "high"},
        headers=h(dorg.admin),
    )
    assert (
        upd.status_code == 200 and upd.json()["target_hours"] == "12.00" and upd.json()["priority"] == "high"
    )
    listed = await c.get(f"{SLA}/targets", params={"subject_type": VR[0]})
    assert [t["id"] for t in listed.json()] == [created["id"]]
    assert (await c.delete(f"{SLA}/targets/{created['id']}", headers=h(dorg.admin))).status_code == 204


async def test_precedence_scope_over_priority_over_generic(dorg: DOrg, vendor_milestone: Any) -> None:
    vendor = uuid.uuid4()
    await _post_target(dorg, target_hours="48")
    await _post_target(dorg, target_hours="30", priority="high")
    await _post_target(dorg, target_hours="24", scope={"vendor_id": str(vendor)})
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    start = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    async with tenant_session(dorg.tenant_id) as db:
        scoped = await sla_service.start_clock(
            ctx,
            db,
            *VR[:1],
            uuid.uuid4(),
            VR[1],
            started_at=start,
            scope={"vendor_id": vendor, "priority": "high"},
        )
        prio = await sla_service.start_clock(
            ctx, db, VR[0], uuid.uuid4(), VR[1], started_at=start, scope={"priority": "high"}
        )
        generic = await sla_service.start_clock(
            ctx, db, VR[0], uuid.uuid4(), VR[1], started_at=start, scope={}
        )
    assert scoped is not None and scoped.target_hours == 24
    assert prio is not None and prio.target_hours == 30
    assert generic is not None and generic.target_hours == 48


async def test_no_target_means_no_clock(dorg: DOrg, vendor_milestone: Any) -> None:
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    async with tenant_session(dorg.tenant_id) as db:
        assert await sla_service.start_clock(ctx, db, VR[0], uuid.uuid4(), VR[1]) is None
    assert await scalar(dorg.tenant_id, "SELECT count(*) FROM sla_clocks") == 0


async def test_business_hours_due_across_weekend_and_holiday(dorg: DOrg, vendor_milestone: Any) -> None:
    # Fri 2026-10-02 is a holiday; start Thu 2026-10-01 17:00 IST with 4 business hours:
    # 1 h on Thu, Fri holiday, weekend, 3 h on Mon 2026-10-05 -> 12:00 IST.
    await set_work_week(dorg.tenant_id, [date(2026, 10, 2)])
    await _post_target(dorg, target_hours="4", calendar="business")
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    start = datetime(2026, 10, 1, 11, 30, tzinfo=UTC)  # 17:00 IST
    async with tenant_session(dorg.tenant_id) as db:
        clock = await sla_service.start_clock(ctx, db, VR[0], uuid.uuid4(), VR[1], started_at=start)
    assert clock is not None
    assert clock.due_at == datetime(2026, 10, 5, 6, 30, tzinfo=UTC)  # 12:00 IST Monday


async def test_vendor_target_at_risk_then_breached_once_and_summary(
    app: FastAPI, dorg: DOrg, vendor_milestone: Any, recipients_box: dict[str, Any]
) -> None:
    """Acceptance: a Vendor-specific 24 h target goes At Risk at 18 h (75 %) and notifies once, then Breached at
    24 h; ``GET /sla/summary`` counts it for that Vendor."""
    lead = await member(app, dorg, "recruiter", name="Vera Vendor Manager")
    recipients_box["users"] = {lead.user_id}
    vendor, other_vendor = uuid.uuid4(), uuid.uuid4()
    await _post_target(
        dorg, target_hours="24", calendar="calendar", at_risk_percent=75, scope={"vendor_id": str(vendor)}
    )
    seen: list[SlaClockStatusChanged] = []
    subscribe(SlaClockStatusChanged, seen.append)
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    start = datetime.now(UTC) - timedelta(hours=1)
    subject = uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:
        clock = await sla_service.start_clock(
            ctx, db, VR[0], subject, VR[1], started_at=start, scope={"vendor_id": vendor}
        )
        assert clock is not None
    for hours, expected in (
        (17, "healthy"),
        (18.5, "at_risk"),
        (19, "at_risk"),
        (24.5, "breached"),
        (30, "breached"),
    ):
        async with tenant_session(dorg.tenant_id) as db:
            await evaluator.evaluate_clocks(db, now=start + timedelta(hours=hours))
        async with tenant_session(dorg.tenant_id) as db:
            row = await db.get(SlaClock, clock.id)
            assert row is not None and row.status == expected, (hours, row.status if row else None)
    mine = [e for e in seen if e.clock_id == clock.id]
    assert [(e.from_status, e.to_status) for e in mine] == [("healthy", "at_risk"), ("at_risk", "breached")]
    assert mine[0].scope_type == "vendor" and mine[0].scope_id == vendor
    n_risk = await scalar(
        dorg.tenant_id,
        "SELECT count(*) FROM notifications WHERE type = 'sla_clock_at_risk' AND user_id = :u",
        u=lead.user_id,
    )
    n_breach = await scalar(
        dorg.tenant_id,
        "SELECT count(*) FROM notifications WHERE type = 'sla_clock_breached' AND user_id = :u",
        u=lead.user_id,
    )
    assert (n_risk, n_breach) == (1, 1)
    link = await scalar(
        dorg.tenant_id, "SELECT link_path FROM notifications WHERE type = 'sla_clock_breached'"
    )
    assert link == f"/vendors/releases/{subject}"

    # a breach survives completion
    async with tenant_session(dorg.tenant_id) as db:
        await sla_service.stop_clock(ctx, db, VR[0], subject, VR[1])
    resp = await dorg.admin.client.get(
        f"{SLA}/summary", params={"subject_type": VR[0], "milestone": VR[1], "vendor_id": str(vendor)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"]["breached"] == 1 and resp.json()["total"] == 1
    other = await dorg.admin.client.get(
        f"{SLA}/summary", params={"subject_type": VR[0], "vendor_id": str(other_vendor)}
    )
    assert other.json()["total"] == 0
    clocks = await dorg.admin.client.get(
        f"{SLA}/clocks", params={"subject_type": VR[0], "subject_id": str(subject)}
    )
    [view] = clocks.json()
    assert (
        view["status"] == "breached" and view["completed_at"] is not None and view["breached_at"] is not None
    )
    await lead.client.aclose()


async def test_met_and_cancel_keep_breach(dorg: DOrg, vendor_milestone: Any) -> None:
    await _post_target(dorg, target_hours="10", calendar="calendar")
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    now = datetime.now(UTC)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:
        await sla_service.start_clock(ctx, db, VR[0], a, VR[1], started_at=now - timedelta(hours=1))
        await sla_service.start_clock(ctx, db, VR[0], b, VR[1], started_at=now - timedelta(hours=1))
        await sla_service.start_clock(ctx, db, VR[0], c, VR[1], started_at=now - timedelta(hours=20))
    async with tenant_session(dorg.tenant_id) as db:
        await evaluator.evaluate_clocks(db)
        await sla_service.stop_clock(ctx, db, VR[0], a, VR[1])
        await sla_service.cancel_clock(ctx, db, VR[0], b, VR[1])
        await sla_service.cancel_clock(ctx, db, VR[0], c, VR[1])
    async with tenant_session(dorg.tenant_id) as db:
        status = {r.subject_id: r.status for r in (await db.execute(select(SlaClock))).scalars()}
    assert status == {a: "met", b: "cancelled", c: "breached"}


async def test_org_rls_vendor_sees_only_its_clocks(dorg: DOrg, vendor_milestone: Any) -> None:
    await _post_target(dorg, target_hours="24")
    ctx = RequestContext(tenant_id=dorg.tenant_id)
    vendor_a, vendor_b = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:
        await sla_service.start_clock(ctx, db, VR[0], uuid.uuid4(), VR[1], scope={"vendor_id": vendor_a})
        await sla_service.start_clock(ctx, db, VR[0], uuid.uuid4(), VR[1], scope={"vendor_id": vendor_b})
        await sla_service.start_clock(ctx, db, VR[0], uuid.uuid4(), VR[1], scope={})
    async with tenant_session(dorg.tenant_id, org=OrgScope.of("vendor", vendor_a)) as db:
        seen = list((await db.execute(select(SlaClock.org_vendor_id))).scalars())
    assert seen == [vendor_a]
    async with tenant_session(dorg.tenant_id) as db:
        assert len(list((await db.execute(select(SlaClock.id))).scalars())) == 3


async def test_permissions_and_cross_tenant(
    app: FastAPI, dorg: DOrg, other_dorg: DOrg, vendor_milestone: Any
) -> None:
    target = await _post_target(dorg)
    rec = await member(app, dorg, "recruiter")
    assert (await rec.client.get(f"{SLA}/targets")).status_code == 403
    assert (await rec.client.post(f"{SLA}/targets", json={}, headers=h(rec))).status_code == 403
    await rec.client.aclose()
    o = other_dorg.admin
    assert (
        await o.client.patch(f"{SLA}/targets/{target['id']}", json={"target_hours": "3"}, headers=h(o))
    ).status_code == 404
    assert (await o.client.delete(f"{SLA}/targets/{target['id']}", headers=h(o))).status_code == 404
    assert (await o.client.get(f"{SLA}/targets")).json() == []
