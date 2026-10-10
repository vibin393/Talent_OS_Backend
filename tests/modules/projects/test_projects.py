# ruff: noqa: E501
"""Epic 45 A2 (FR-228/229): projects under a SOW, roles + skills, Demands from roles, assignments through
``link_project``, the resource timeline, ``ProjectAssignmentEnded`` and ``project_context``; scope / isolation."""

import uuid
from datetime import date
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.projects import ProjectAssignmentEnded
from app.services import projects_service
from app.services.projects import assignments
from tests.modules.commercials.helpers import create_contract
from tests.modules.identity.conftest import join
from tests.modules.projects.helpers import (
    API,
    COrg,
    active_sow,
    assign,
    candidate,
    client_user,
    err,
    ok,
    placement,
    project,
    role,
)


async def _skill_key(org: COrg, name: str) -> str:
    from app.services.candidates.skills import resolve_skill

    async with tenant_session(org.tenant_id) as db:
        ref = await resolve_skill(org.ctx(), db, name, create_pending=True)
        assert ref is not None
        return f"{ref.kind}:{ref.id}"


async def test_project_activation_needs_active_sow_scope_and_isolation(
    app: FastAPI, corg: COrg, other_corg: COrg
) -> None:
    sow = await active_sow(corg)
    prj = await project(corg, sow["id"], activate=False, start_date="2026-01-01", end_date="2026-12-31")
    assert prj["status"] == "draft" and prj["human_id"].startswith("PRJ-")
    assert prj["contract_human_id"] == sow["human_id"] and prj["currency"] == "INR"
    # no SOW -> 422; SOW not active -> 409
    bare = await project(corg, activate=False, name="No SOW")
    err(await corg.c.post(f"{API}/projects/{bare['id']}/transition", json={"to": "active"}, headers=corg.h()), 422, "sow_required")
    draft_sow = await create_contract(
        corg, contract_type="sow", title="Draft SOW", parent_contract_id=sow["parent_contract_id"], pricing_model="fixed_price", value_cap="10"
    )
    pending = await project(corg, draft_sow["id"], activate=False, name="Pending")
    err(
        await corg.c.post(f"{API}/projects/{pending['id']}/transition", json={"to": "active"}, headers=corg.h()),
        409,
        "contract_not_active",
    )
    active = await ok(await corg.c.post(f"{API}/projects/{prj['id']}/transition", json={"to": "active"}, headers=corg.h()))
    assert active["status"] == "active"
    err(await corg.c.post(f"{API}/projects/{prj['id']}/transition", json={"to": "draft"}, headers=corg.h()), 409, "transition_not_allowed")
    # a SOW of another client is refused
    other_client = await corg.c.post(f"{API}/clients", json={"name": "Other Co"}, headers=corg.h())
    bad = await corg.c.post(
        f"{API}/projects",
        json={"name": "x", "client_id": other_client.json()["id"], "contract_id": sow["id"]},
        headers=corg.h(),
    )
    err(bad, 422, "contract_invalid")
    # cross-tenant 404; a Delivery Manager without the project in scope gets 404 and an empty list
    assert (await other_corg.c.get(f"{API}/projects/{prj['id']}")).status_code == 404
    dm = await join(app, corg.tenant, "delivery_manager", name="Dee Manager")
    try:
        assert (await dm.client.get(f"{API}/projects/{prj['id']}")).status_code == 404
        assert await ok(await dm.client.get(f"{API}/projects")) == []
        mine = await ok(
            await dm.client.post(f"{API}/projects", json={"name": "Mine", "client_id": str(corg.client_id)}, headers=dm.h()),
            201,
        )
        assert [p["id"] for p in await ok(await dm.client.get(f"{API}/projects"))] == [mine["id"]]
    finally:
        await dm.client.aclose()
    # portal cookies never reach internal routes
    cu = await client_user(app, corg)
    try:
        assert (await cu.client.get(f"{API}/projects")).status_code in (401, 403)
    finally:
        await cu.client.aclose()
    listed = await ok(await corg.c.get(f"{API}/projects", params={"q": prj["human_id"]}))
    assert [p["id"] for p in listed] == [prj["id"]]
    hits = await _quick(corg, prj["human_id"])
    assert hits[0].exact and hits[0].type == "project"


async def _quick(org: COrg, q: str) -> list[Any]:
    async with tenant_session(org.tenant_id) as db:
        return await projects_service.quick_search(org.ctx(), db, q, 5)


async def test_roles_skills_and_demand_from_role(corg: COrg) -> None:
    sow = await active_sow(corg)
    prj = await project(corg, sow["id"], start_date="2026-01-01", end_date="2026-12-31")
    err(
        await corg.c.post(
            f"{API}/projects/{prj['id']}/roles",
            json={"title": "Dev", "skills": [{"skill_key": f"global:{uuid.uuid4()}"}]},
            headers=corg.h(),
        ),
        422,
        "skill_invalid",
    )
    java = await _skill_key(corg, "Java Epic45")
    r = await role(
        corg, prj["id"], headcount=3, start_date="2026-02-01", skills=[{"skill_key": java, "min_proficiency": 4}], bill_rate="1500", bill_rate_unit="hour"
    )
    assert r["skills"][0]["skill_key"] == java and r["skills"][0]["min_proficiency"] == 4
    assert r["skills"][0]["label"] == "Java Epic45" and r["status"] == "open"
    p1 = await placement(corg)
    await ok(await assign(corg, r["id"], p1), 201)
    demand = await ok(await corg.c.post(f"{API}/projects/{prj['id']}/roles/{r['id']}/demand", json={}, headers=corg.h()), 201)
    assert demand["project_role_id"] == r["id"] and demand["positions"] == 2 and demand["status"] == "draft"
    async with tenant_session(corg.tenant_id) as db:
        rows = (
            await db.execute(text("SELECT skill_label, skill_ref_kind, skill_id FROM demand_skills WHERE demand_id = :d"), {"d": demand["id"]})
        ).all()
    assert [(x.skill_label, f"{x.skill_ref_kind}:{x.skill_id}") for x in rows] == [("Java Epic45", java)]
    roles_out = await ok(await corg.c.get(f"{API}/projects/{prj['id']}/roles"))
    assert roles_out[0]["demand_ids"] == [demand["id"]] and roles_out[0]["filled_fte"] == "1.00"
    # a role with a demand / assignment cannot be deleted
    err(await corg.c.delete(f"{API}/project-roles/{r['id']}", headers=corg.h()), 409, "role_in_use")
    spare = await role(corg, prj["id"], title="Spare")
    assert (await corg.c.delete(f"{API}/project-roles/{spare['id']}", headers=corg.h())).status_code == 204


async def test_assignments_link_placements_timeline_and_end_event(corg: COrg) -> None:
    sow = await active_sow(corg)
    prj_a = await project(corg, sow["id"], name="A", start_date="2026-01-01", end_date="2026-12-31")
    prj_b = await project(corg, sow["id"], name="B", start_date="2026-01-01", end_date="2026-12-31")
    role_a = await role(corg, prj_a["id"], headcount=2, start_date="2026-02-01", end_date="2026-06-30")
    role_b = await role(corg, prj_b["id"], start_date="2026-02-01", end_date="2026-06-30")
    worker = await candidate(corg, "Wanda Worker")
    p_a, p_b = await placement(corg, worker), await placement(corg, worker)
    # placement of another client
    other_client = uuid.UUID((await corg.c.post(f"{API}/clients", json={"name": "Elsewhere"}, headers=corg.h())).json()["id"])
    stranger = await placement(corg, client_id=other_client)
    err(await assign(corg, role_a["id"], stranger), 422, "placement_client_mismatch")
    ended = await placement(corg, status="ended")
    err(await assign(corg, role_a["id"], ended), 409, "placement_closed")

    a1 = await ok(await assign(corg, role_a["id"], p_a, pct="60", start="2026-03-01"), 201)
    assert a1["status"] == "active" and a1["worker_name"] == "Wanda Worker" and a1["warnings"] == []
    err(await assign(corg, role_b["id"], p_a, pct="10"), 409, "placement_already_assigned")
    a2 = await ok(await assign(corg, role_b["id"], p_b, pct="50", start="2026-01-15"), 201)
    assert a2["warnings"] == ["assignment_outside_role_dates"]
    async with tenant_session(corg.tenant_id) as db:
        link = (await db.execute(text("SELECT project_id, project_role_id FROM placements WHERE id = :p"), {"p": p_a})).one()
    assert (str(link.project_id), str(link.project_role_id)) == (prj_a["id"], role_a["id"])

    tl = await ok(await corg.c.get(f"{API}/projects/{prj_a['id']}/resource-timeline", params={"from": "2026-02-01", "to": "2026-06-30"}))
    segs = tl["roles"][0]["segments"]
    assert segs[0] == {"start": "2026-02-01", "end": "2026-02-28", "assigned_fte": "0.00", "headcount": 2, "gap": True}
    assert segs[1]["assigned_fte"] == "0.60" and segs[1]["gap"] is True and tl["gap_count"] == 2
    (w,) = tl["workers"]
    over = [s for s in w["segments"] if s["over"]]
    assert over and over[0]["total_pct"] == "110.00" and over[0]["start"] == "2026-03-01"
    assert {s["project_name"] for s in over[0]["split"]} == {"A", "B"} and tl["over_allocation_count"] == 1
    p360 = await ok(await corg.c.get(f"{API}/projects/{prj_a['id']}/360"))
    assert {r["kind"] for r in p360["risks"]} >= {"over_allocation"} and p360["sow"]["pricing_model"] == "milestone"
    assert p360["sow"]["total_value"] == {"amount": "1000000.00", "currency": "INR"}

    async with tenant_session(corg.tenant_id) as db:
        ctx_out = await projects_service.project_context(corg.ctx(), db, uuid.UUID(prj_a["id"]))
    assert ctx_out.roles[0].filled_count == 1 and ctx_out.contract_id == uuid.UUID(sow["id"])

    events: list[ProjectAssignmentEnded] = []
    subscribe(ProjectAssignmentEnded, events.append)
    try:
        done = await ok(
            await corg.c.post(f"{API}/project-assignments/{a1['id']}/end", json={"end_date": "2026-04-30", "reason": "Rolled off"}, headers=corg.h())
        )
        assert done["status"] == "ended" and done["end_date"] == "2026-04-30"
        again = await ok(
            await corg.c.post(f"{API}/project-assignments/{a1['id']}/end", json={"end_date": "2026-05-30"}, headers=corg.h())
        )
        assert again["end_date"] == "2026-04-30"
        assert len(events) == 1 and events[0].ended_on == date(2026, 4, 30) and events[0].candidate_id == worker
        async with tenant_session(corg.tenant_id) as db:
            link = (await db.execute(text("SELECT project_id FROM placements WHERE id = :p"), {"p": p_a})).scalar_one()
        assert link is None
        # PlacementEnded handler path: ends the remaining active assignment exactly once
        async with tenant_session(corg.tenant_id) as db:
            assert await assignments.end_for_placement(corg.ctx(), db, p_b, date(2026, 5, 31), "Placement ended") == 1
        async with tenant_session(corg.tenant_id) as db:
            assert await assignments.end_for_placement(corg.ctx(), db, p_b, date(2026, 5, 31), "Placement ended") == 0
        assert len(events) == 2 and events[1].placement_id == p_b
    finally:
        unsubscribe(ProjectAssignmentEnded, events.append)
    roles_out = await ok(await corg.c.get(f"{API}/projects/{prj_b['id']}/roles"))
    assert roles_out[0]["status"] == "open" and roles_out[0]["active_assignments"] == 0


async def test_role_status_derives_filled(corg: COrg) -> None:
    sow = await active_sow(corg)
    prj = await project(corg, sow["id"])
    r = await role(corg, prj["id"], headcount=1)
    p1 = await placement(corg)
    a = await ok(await assign(corg, r["id"], p1), 201)
    assert (await ok(await corg.c.get(f"{API}/projects/{prj['id']}/roles")))[0]["status"] == "filled"
    err(await corg.c.post(f"{API}/projects/{prj['id']}/roles/{r['id']}/demand", json={}, headers=corg.h()), 409, "role_filled")
    closed = await ok(await corg.c.patch(f"{API}/project-roles/{r['id']}", json={"status": "closed"}, headers=corg.h()))
    assert closed["status"] == "closed"
    await ok(await corg.c.post(f"{API}/project-assignments/{a['id']}/end", json={"end_date": "2026-03-01"}, headers=corg.h()))
    assert (await ok(await corg.c.get(f"{API}/projects/{prj['id']}/roles")))[0]["status"] == "closed"
    reopened = await ok(await corg.c.patch(f"{API}/project-roles/{r['id']}", json={"status": "open"}, headers=corg.h()))
    assert reopened["status"] == "open"


async def test_importers(corg: COrg) -> None:
    from app.core.errors import ValidationFailed
    from app.schemas.projects import ProjectAssignmentImportIn, ProjectImportIn, ProjectRoleImportIn

    sow = await active_sow(corg)
    client_name = (await ok(await corg.c.get(f"{API}/clients/{corg.client_id}")))["name"]
    p1 = await placement(corg)
    async with tenant_session(corg.tenant_id) as db:
        data = ProjectImportIn(name="Imported", client_name=client_name, sow_human_id=sow["human_id"], status="active")
        assert await projects_service.import_project(corg.ctx(), db, data, validate_only=True) is None
        pid = await projects_service.import_project(corg.ctx(), db, data)
        human = (await db.execute(text("SELECT human_id FROM projects WHERE id = :i"), {"i": pid})).scalar_one()
        rid = await projects_service.import_project_role(corg.ctx(), db, ProjectRoleImportIn(project_human_id=human, title="QA", headcount=2))
        assert rid is not None
        placement_human = (await db.execute(text("SELECT human_id FROM placements WHERE id = :i"), {"i": p1})).scalar_one()
        aid = await projects_service.import_project_assignment(
            corg.ctx(),
            db,
            ProjectAssignmentImportIn(project_human_id=human, role_title="QA", placement_human_id=placement_human, start_date=date(2026, 2, 1)),
        )
        assert aid is not None
        try:
            await projects_service.import_project(corg.ctx(), db, ProjectImportIn(name="x", client_name="Nope Ltd"))
        except ValidationFailed as exc:
            assert exc.details["fields"][0]["field"] == "client_name"
        else:  # pragma: no cover
            raise AssertionError("expected a validation error")
