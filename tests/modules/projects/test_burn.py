# ruff: noqa: E501
"""Epic 45 A4 (FR-230, AD-33): budgets, the burn rebuild from posted facts only, identical figures on a second
rebuild and the 80 % / 100 % alerts once per budget version."""

import uuid
from datetime import UTC, datetime

from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.projects import ProjectBudgetThresholdReached
from app.models.postings import PayablePosting
from tests.modules.projects.helpers import API, COrg, active_sow, assign, ok, placement, project, role


async def _posting(org: COrg, placement_id: uuid.UUID, amount: str, state: str) -> None:
    async with tenant_session(org.tenant_id) as db:
        db.add(
            PayablePosting(
                id=uuid.uuid4(),
                tenant_id=org.tenant_id,
                source_kind="timesheet_entry",
                source_id=uuid.uuid4(),
                target_kind="payroll_row",
                target_id=uuid.uuid4(),
                placement_id=placement_id,
                quantity=1,
                amount=amount,
                currency="INR",
                state=state,
                posted_at=datetime(2026, 3, 10, 6, 0, tzinfo=UTC) if state == "posted" else None,
                created_by_kind="system",
                payee_kind="worker",
            )
        )


async def test_burn_posted_only_identical_rebuild_and_alerts_once(corg: COrg) -> None:
    sow = await active_sow(corg)
    prj = await project(corg, sow["id"], start_date="2026-01-01", end_date="2026-12-31")
    r = await role(corg, prj["id"])
    p1 = await placement(corg)
    await ok(await assign(corg, r["id"], p1, pct="50", start="2026-02-01"), 201)
    budgets = await ok(
        await corg.c.put(
            f"{API}/projects/{prj['id']}/budgets",
            json={"budgets": [{"dimension": "cost", "amount": "1000"}, {"dimension": "hours", "amount": "100"}]},
            headers=corg.h(),
        )
    )
    assert {(b["dimension"], b["currency"], b["version"]) for b in budgets} == {("cost", "INR", 1), ("hours", None, 1)}
    await _posting(corg, p1, "1700.00", "posted")  # 50 % -> 850 (85 %)
    await _posting(corg, p1, "9999.00", "reserved")  # never counts

    events: list[ProjectBudgetThresholdReached] = []
    subscribe(ProjectBudgetThresholdReached, events.append)
    try:
        burn = await ok(await corg.c.post(f"{API}/projects/{prj['id']}/burn/rebuild", headers=corg.h()))
        cost = next(b for b in burn if b["dimension"] == "cost")
        assert cost["actual"] == "850.00" and cost["pct"] == "85.0000" and cost["currency"] == "INR"
        assert next(b for b in burn if b["dimension"] == "hours")["actual"] == "0.00"
        again = await ok(await corg.c.post(f"{API}/projects/{prj['id']}/burn/rebuild", headers=corg.h()))
        assert again == burn
        assert [(e.dimension, e.threshold) for e in events] == [("cost", 80)]
        # crossing 100 % alerts once more; a new budget version re-arms the alerts
        await _posting(corg, p1, "400.00", "posted")
        await ok(await corg.c.post(f"{API}/projects/{prj['id']}/burn/rebuild", headers=corg.h()))
        await ok(await corg.c.post(f"{API}/projects/{prj['id']}/burn/rebuild", headers=corg.h()))
        assert [(e.dimension, e.threshold) for e in events] == [("cost", 80), ("cost", 100)]
        bumped = await ok(
            await corg.c.put(f"{API}/projects/{prj['id']}/budgets", json={"budgets": [{"dimension": "cost", "amount": "1100"}]}, headers=corg.h())
        )
        cost_budget = next(b for b in bumped if b["dimension"] == "cost")
        assert cost_budget["version"] == 2 and cost_budget["alert_80_at"] is None
        await ok(await corg.c.post(f"{API}/projects/{prj['id']}/burn/rebuild", headers=corg.h()))
        assert [(e.threshold, e.budget_version) for e in events][2:] == [(80, 2)]  # 1050 / 1100 = 95 %
    finally:
        unsubscribe(ProjectBudgetThresholdReached, events.append)
    p360 = await ok(await corg.c.get(f"{API}/projects/{prj['id']}/360"))
    assert any(r["kind"] == "budget_threshold" for r in p360["risks"])
    listed = await ok(await corg.c.get(f"{API}/projects"))
    assert next(p for p in listed if p["id"] == prj["id"])["burn_pct"] is not None
