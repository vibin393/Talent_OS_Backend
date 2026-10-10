# ruff: noqa: E501
"""Epic 15 story 2.5 (A7): sales dashboard tiles vs fixtures, data scope, tile links; erasure of lead contacts."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.core.database import tenant_session
from app.models.crm import Opportunity
from tests.modules.crm.conftest import API, CrmOrg, JoinAs, create_client, create_lead, create_opp, h, move, ok, transition_lead


async def test_dashboard_tiles_and_links(corg: CrmOrg) -> None:
    c, hh = corg.admin.client, h(corg.admin)
    client = await create_client(corg.admin, "Dash Co")
    today = datetime.now(UTC).date()
    a = await create_opp(corg.admin, client["id"], "A", estimated_revenue="1000.00", expected_close_date=today.isoformat())
    await create_opp(corg.admin, client["id"], "B", estimated_revenue="500.00", currency="USD", expected_close_date=today.isoformat())
    await create_opp(corg.admin, client["id"], "Overdue", estimated_revenue="10.00", expected_close_date=(today - timedelta(days=40)).isoformat())
    won = await create_opp(corg.admin, client["id"], "W", estimated_revenue="10.00")
    lost = await create_opp(corg.admin, client["id"], "L", estimated_revenue="10.00")
    await ok(await move(corg.admin, a, "proposal"))
    win = await c.post(f"{API}/opportunities/{won['id']}/win", json={"activate_client": False}, headers={**hh, "Idempotency-Key": "d1"})
    assert win.status_code == 200, win.text
    await ok(await move(corg.admin, lost, "lost", reason_code="price"))
    async with tenant_session(corg.tenant_id) as db:  # a 10-day sales cycle
        await db.execute(
            update(Opportunity).where(Opportunity.id == uuid.UUID(won["id"])).values(created_at=datetime.now(UTC) - timedelta(days=10))
        )
    # leads by source
    l1 = await create_lead(corg.admin, "Lead One", source_code="referral")
    await create_lead(corg.admin, "Lead Two", source_code="referral")
    await ok(await transition_lead(corg.admin, l1["id"], "qualified"))
    conv = {
        "client": {"mode": "create", "name": "Lead One Client"},
        "contact": {"mode": "none"},
        "opportunity": {"name": "From lead"},
    }
    await ok(await c.post(f"{API}/leads/{l1['id']}/convert", json=conv, headers=hh), 201)
    # an overdue task on a lead
    task = await c.post(
        f"{API}/tasks",
        json={"title": "Chase", "entity_type": "lead", "entity_id": l1["id"], "due_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
        headers=hh,
    )
    assert task.status_code == 201, task.text

    d = await ok(await c.get(f"{API}/crm/dashboard"))
    stages = {s["stage"]: s for s in d["pipeline"]["by_stage"]}
    assert stages["proposal"]["count"] == 1
    assert stages["proposal"]["totals"] == [{"currency": "INR", "estimated": "1000.00", "weighted": "400.00"}]
    assert stages["qualification"]["count"] == 3  # B, Overdue, From lead
    months = {m["month"]: m for m in d["pipeline"]["by_close_month"]}
    assert len(months) == 7 and months["overdue"]["count"] == 1
    closing = d["closing_this_month"]
    assert closing["count"] == 2
    assert {t["currency"] for t in closing["totals"]} == {"INR", "USD"}
    assert "close_from=" in closing["link"] and "stage=qualification" in closing["link"]
    # the linked list shows the same count
    list_page = await ok(await c.get(f"{API}{closing['link'].replace('view=list&', '')}"))
    assert list_page["total"] == closing["count"]
    wr = d["win_rate"]
    assert (wr["won"], wr["lost"], wr["win_rate_pct"]) == (1, 1, "50.0")
    assert wr["avg_sales_cycle_days"] is not None and float(wr["avg_sales_cycle_days"]) >= 9.9
    src = {r["source_code"]: r for r in d["leads_by_source"]["rows"]}
    assert (src["referral"]["created"], src["referral"]["converted"], src["referral"]["conversion_rate_pct"]) == (2, 1, "50.0")
    assert d["overdue_tasks"]["count"] == 1


async def test_dashboard_scope_and_permission(corg: CrmOrg, join_as: JoinAs) -> None:
    rep = await join_as("sales", name="Rita Rep")
    client = await create_client(rep, "Scope Co")
    await create_opp(rep, client["id"], "Rep deal")
    await create_opp(corg.admin, client["id"], "Admin deal")
    mine = await ok(await rep.client.get(f"{API}/crm/dashboard"))
    assert sum(s["count"] for s in mine["pipeline"]["by_stage"]) == 1
    everyone = await ok(await corg.admin.client.get(f"{API}/crm/dashboard"))
    assert sum(s["count"] for s in everyone["pipeline"]["by_stage"]) == 2
    only_rep = await ok(await corg.admin.client.get(f"{API}/crm/dashboard", params={"owner_ids": str(rep.user_id)}))
    assert sum(s["count"] for s in only_rep["pipeline"]["by_stage"]) == 1
    recruiter = await join_as("recruiter", name="Ron Recruiter")
    resp = await recruiter.client.get(f"{API}/crm/dashboard")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] in ("forbidden", "permission_denied", "missing_permission")


async def test_lead_contact_erasure(corg: CrmOrg) -> None:
    from app.core.context import RequestContext
    from app.models.candidates import Candidate, CandidateEmail
    from app.models.crm import Lead
    from app.services.crm_erasure import CrmLeadsHandler
    from app.services.privacy.registry import ErasureContext, handlers

    assert "crm.leads" in {x.name for x in handlers()}
    lead = await create_lead(corg.admin, "Erase Co", contact_name="Pria Person", contact_email="pria@example.com")
    async with tenant_session(corg.tenant_id) as db:
        cand = Candidate(id=uuid.uuid4(), tenant_id=corg.tenant_id, human_id=f"CAN-{uuid.uuid4().hex[:6]}", full_name="Pria Person", source="manual", owner_id=corg.admin.user_id)
        db.add(cand)
        await db.flush()
        db.add(CandidateEmail(id=uuid.uuid4(), tenant_id=corg.tenant_id, candidate_id=cand.id, email="pria@example.com"))
        await db.flush()
        ectx = ErasureContext(tenant_id=corg.tenant_id, candidate_id=cand.id, run_id=uuid.uuid4(), reason="dsr")
        handler = CrmLeadsHandler()
        assert (await handler.export(db, ectx)) is not None
        assert await handler.erase(db, ectx) == {"leads": 1}
        assert await handler.verify(db, ectx) == []
        row = await db.get(Lead, uuid.UUID(lead["id"]))
        assert row is not None and (row.contact_name, row.contact_email) == ("[erased]", None)
    _ = RequestContext
