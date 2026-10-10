"""Story 4.6: SLA settings, clocks on Open, evaluation task (status changes, single emission, idempotent)."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.demands import DemandSlaStatusChanged
from app.models.demands import DemandSlaClock
from app.tasks.demands import evaluate_slas_async
from tests.modules.demands.helpers import DEMANDS, DOrg, h, open_demand

SETTINGS = "/api/v1/settings"
EVERY_DAY = {d: {"start": "00:00", "end": "24:00"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


async def _targets(org: DOrg, targets: list[dict[str, object]]) -> None:
    resp = await org.admin.client.put(
        f"{SETTINGS}/sla-targets", json={"targets": targets}, headers=h(org.admin)
    )
    assert resp.status_code == 200, resp.text


async def _shift_clock(org: DOrg, demand_id: str, milestone: str, hours: float) -> None:
    """Move the clock's start (and due) back in time by ``hours``."""
    async with tenant_session(org.tenant_id) as db:
        clock = (
            await db.execute(
                select(DemandSlaClock).where(
                    DemandSlaClock.demand_id == uuid.UUID(demand_id), DemandSlaClock.milestone == milestone
                )
            )
        ).scalar_one()
        clock.started_at -= timedelta(hours=hours)
        clock.due_at -= timedelta(hours=hours)


async def test_settings_validation(dorg: DOrg) -> None:
    m = dorg.admin
    bad = await m.client.put(
        f"{SETTINGS}/sla-targets",
        json={"targets": [{"milestone": "assignment", "target_hours": 8, "at_risk_percent": 100}]},
        headers=h(m),
    )
    assert bad.status_code == 422
    dup = await m.client.put(
        f"{SETTINGS}/sla-targets",
        json={
            "targets": [
                {"milestone": "assignment", "target_hours": 8},
                {"milestone": "assignment", "target_hours": 4},
            ]
        },
        headers=h(m),
    )
    assert (dup.status_code, dup.json()["error"]["code"]) == (422, "duplicate_sla_target")
    cal = await m.client.put(
        f"{SETTINGS}/business-calendar",
        json={"work_week": {"mon": {"start": "10:00", "end": "09:00"}}, "holidays": []},
        headers=h(m),
    )
    assert cal.status_code == 422
    ok = await m.client.put(
        f"{SETTINGS}/business-calendar",
        json={"work_week": {"mon": {"start": "09:00", "end": "17:00"}}, "holidays": ["2026-12-25"]},
        headers=h(m),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["holidays"] == ["2026-12-25"] and ok.json()["timezone"] == "Asia/Kolkata"
    assert (await m.client.get(f"{SETTINGS}/business-calendar")).json()["work_week"] == {
        "mon": {"start": "09:00", "end": "17:00"}
    }


async def test_priority_target_overrides_any(dorg: DOrg) -> None:
    await _targets(
        dorg,
        [
            {"milestone": "assignment", "target_hours": 8, "clock": "calendar"},
            {"milestone": "assignment", "priority": "critical", "target_hours": 2, "clock": "calendar"},
        ],
    )
    crit = await open_demand(dorg.admin, dorg.client_id, priority="critical")
    low = await open_demand(dorg.admin, dorg.client_id, priority="low")
    by = {c["milestone"]: c for c in crit["sla"]}
    assert set(by) == {"assignment"}

    def span(clock: dict[str, str]) -> timedelta:
        return datetime.fromisoformat(clock["due_at"]) - datetime.fromisoformat(clock["started_at"])

    assert span(by["assignment"]) == timedelta(hours=2)
    assert span(low["sla"][0]) == timedelta(hours=8)
    assert crit["sla_status"] == "healthy"


async def test_evaluation_emits_once_per_change(dorg: DOrg) -> None:
    m = dorg.admin
    cal = await m.client.put(
        f"{SETTINGS}/business-calendar", json={"work_week": EVERY_DAY, "holidays": []}, headers=h(m)
    )
    assert cal.status_code == 200, cal.text
    await _targets(
        dorg, [{"milestone": "assignment", "target_hours": 8, "clock": "business", "at_risk_percent": 75}]
    )
    opened = await open_demand(m, dorg.client_id)
    assert [c["status"] for c in opened["sla"]] == ["healthy"]
    events: list[DemandSlaStatusChanged] = []
    subscribe(DemandSlaStatusChanged, events.append)
    try:
        assert await evaluate_slas_async(dorg.tenant_id) == 0
        await _shift_clock(dorg, opened["id"], "assignment", 6.5)  # 6.5 of 8 h -> 81 % -> at risk
        assert await evaluate_slas_async(dorg.tenant_id) == 1
        assert await evaluate_slas_async(dorg.tenant_id) == 0  # idempotent re-run
        assert [(e.milestone, e.status) for e in events] == [("assignment", "at_risk")]
        assert dorg.admin.user_id in events[0].recipient_ids  # tenant admin holds demand:assign tenant-wide
        body = (await m.client.get(f"{DEMANDS}/{opened['id']}")).json()
        assert body["sla_status"] == "at_risk"
        at_risk = (await m.client.get(DEMANDS, params={"sla_status": "at_risk"})).json()["items"]
        assert [i["id"] for i in at_risk] == [opened["id"]]

        await _shift_clock(dorg, opened["id"], "assignment", 2)
        assert await evaluate_slas_async(dorg.tenant_id) == 1
        assert events[-1].status == "breached"

        # Assigning a primary completes the clock (late -> breached, no more evaluation).
        await m.client.put(
            f"{DEMANDS}/{opened['id']}/assignments",
            json={"user_ids": [str(m.user_id)], "primary_user_id": str(m.user_id)},
            headers=h(m),
        )
        body = (await m.client.get(f"{DEMANDS}/{opened['id']}")).json()
        assert body["sla"][0]["completed_at"] is not None
        assert await evaluate_slas_async(dorg.tenant_id) == 0
    finally:
        unsubscribe(DemandSlaStatusChanged, events.append)


async def test_primary_in_time_is_met_and_closed_demands_ignored(dorg: DOrg) -> None:
    m = dorg.admin
    await _targets(dorg, [{"milestone": "assignment", "target_hours": 8, "clock": "calendar"}])
    a = await open_demand(m, dorg.client_id)
    await m.client.put(
        f"{DEMANDS}/{a['id']}/assignments",
        json={"user_ids": [str(m.user_id)], "primary_user_id": str(m.user_id)},
        headers=h(m),
    )
    body = (await m.client.get(f"{DEMANDS}/{a['id']}")).json()
    assert body["sla"][0]["status"] == "met"
    b = await open_demand(m, dorg.client_id)
    await m.client.post(
        f"{DEMANDS}/{b['id']}/transition", json={"to": "closed", "reason_code": "other"}, headers=h(m)
    )
    await _shift_clock(dorg, b["id"], "assignment", 100)
    assert await evaluate_slas_async(dorg.tenant_id) == 0
