# ruff: noqa: E501
"""Epic 45 A3 / A5 (FR-228, FR-231, AD-24 / FR-257): deliverables, ★ acceptance from the Client Portal and by a
recording User, the milestone becoming billable in the same transaction, rejection, the refusal matrix and Client
Portal isolation (cross-Client 404, restricted Client Users, cost-free schemas)."""

import uuid
from datetime import date

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core.actors import ActorRef
from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.core.events import subscribe, unsubscribe
from app.events.projects import DeliverableAccepted
from app.services.projects import deliverables
from tests.modules.commercials.helpers import PDF
from tests.modules.projects.helpers import (
    API,
    CP,
    COrg,
    active_sow,
    assign,
    client_user,
    deliverable,
    err,
    milestone,
    ok,
    placement,
    project,
    role,
    submitted,
)


async def _setup(corg: COrg) -> tuple[dict, dict, dict]:
    sow = await active_sow(corg)
    m = await milestone(corg, sow["id"], "Design", "250000")
    prj = await project(corg, sow["id"], start_date="2026-01-01", end_date="2026-12-31")
    return sow, m, prj


async def _milestone_status(corg: COrg, sow_id: str, mid: str) -> str:
    rows = await ok(await corg.c.get(f"{API}/contracts/{sow_id}/milestones"))
    return next(r["status"] for r in rows if r["id"] == mid)


async def test_milestone_billable_after_portal_and_recorded_acceptance(app: FastAPI, corg: COrg) -> None:
    sow, m, prj = await _setup(corg)
    # a milestone of another contract is refused
    other = await active_sow(corg, title="Other SOW")
    foreign = await milestone(corg, other["id"], "Foreign")
    err(
        await corg.c.post(f"{API}/projects/{prj['id']}/deliverables", json={"name": "x", "milestone_id": foreign["id"]}, headers=corg.h()),
        422,
        "milestone_not_in_sow",
    )
    d1 = await deliverable(corg, prj["id"], name="Spec", milestone_id=m["id"])
    d2 = await deliverable(corg, prj["id"], name="Prototype", milestone_id=m["id"])
    d3 = await deliverable(corg, prj["id"], name="Dropped", milestone_id=m["id"])
    await ok(await corg.c.post(f"{API}/deliverables/{d3['id']}/cancel", headers=corg.h()))
    err(await corg.c.post(f"{API}/deliverables/{d1['id']}/submit", headers=corg.h()), 409, "transition_not_allowed")
    await submitted(corg, d1["id"])
    await submitted(corg, d2["id"])

    events: list[DeliverableAccepted] = []
    subscribe(DeliverableAccepted, events.append)
    cu = await client_user(app, corg)
    try:
        listed = await ok(await cu.client.get(f"{CP}/projects"))
        assert [(p["id"], p["awaiting_acceptance"]) for p in listed] == [(prj["id"], 2)]
        detail = await ok(await cu.client.get(f"{CP}/projects/{prj['id']}"))
        assert [d["status"] for d in detail["deliverables"]][:2] == ["submitted", "submitted"]
        assert detail["milestones"][0]["amount"] == {"amount": "250000.00", "currency": "INR"}
        first = await ok(await cu.client.post(f"{CP}/deliverables/{d1['id']}/accept", headers=cu.h()))
        assert first["status"] == "accepted"
        replay = await ok(await cu.client.post(f"{CP}/deliverables/{d1['id']}/accept", headers=cu.h()))
        assert replay["accepted_at"] == first["accepted_at"]
    finally:
        await cu.client.aclose()
        unsubscribe(DeliverableAccepted, events.append)
    assert len(events) == 1 and events[0].actor_kind == "client_user" and events[0].milestone_id == uuid.UUID(m["id"])
    assert await _milestone_status(corg, sow["id"], m["id"]) == "planned"

    # recorded acceptance needs evidence
    err(await corg.c.post(f"{API}/deliverables/{d2['id']}/record-acceptance", headers=corg.h()), 422, "evidence_required")
    resp = await corg.c.post(
        f"{API}/deliverables/{d2['id']}/record-acceptance",
        files={"file": ("acceptance.pdf", PDF, "application/pdf")},
        headers=corg.h(),
    )
    out = await ok(resp)
    assert out["status"] == "accepted" and out["accepted_actor"]["kind"] == "recorded_by_user"
    assert out["accepted_actor"]["evidence_file_id"] in out["evidence_file_ids"]
    assert await _milestone_status(corg, sow["id"], m["id"]) == "accepted"
    async with tenant_session(corg.tenant_id) as db:
        n = (await db.execute(text("SELECT count(*) FROM deliverable_events WHERE deliverable_id = :d AND kind = 'accepted'"), {"d": d1["id"]})).scalar_one()
        assert n == 1
    # the accepted milestone is billable (milestone posting source)
    from app.services.postings import SourceQuery, posting_sources

    async with tenant_session(corg.tenant_id) as db:
        items = await posting_sources()["milestone"].loader(corg.ctx(), db, SourceQuery(ledger="billing", period_end=date(2099, 1, 1)))
    assert uuid.UUID(m["id"]) in {i.source_id for i in items}


async def test_refusal_matrix_and_rejection(app: FastAPI, corg: COrg) -> None:
    _, _, prj = await _setup(corg)
    d = await deliverable(corg, prj["id"])
    await submitted(corg, d["id"])
    for kind in ("system", "workflow", "ai", "api_key"):
        async with tenant_session(corg.tenant_id) as db:
            row = await deliverables._lock_any(db, uuid.UUID(d["id"]))
            with pytest.raises(Forbidden) as exc:
                await deliverables.accept(corg.ctx(), db, row, ActorRef(kind, None))  # type: ignore[arg-type]
            assert exc.value.code == "human_only_action"
    async with tenant_session(corg.tenant_id) as db:
        row = await deliverables._lock_any(db, uuid.UUID(d["id"]))
        with pytest.raises(Forbidden) as exc:
            await deliverables.accept(corg.ctx(), db, row, ActorRef("user", corg.admin.user_id))
        assert exc.value.code == "acceptance_actor_invalid"
    cu = await client_user(app, corg)
    try:
        err(await cu.client.post(f"{CP}/deliverables/{d['id']}/reject", json={"reason": "no"}, headers=cu.h()), 422, "validation_failed")
        rejected = await ok(
            await cu.client.post(f"{CP}/deliverables/{d['id']}/reject", json={"reason": "Missing the API section"}, headers=cu.h())
        )
        assert rejected["status"] == "in_progress" and rejected["last_rejection_reason"] == "Missing the API section"
        err(await cu.client.post(f"{CP}/deliverables/{d['id']}/accept", headers=cu.h()), 409, "transition_not_allowed")
    finally:
        await cu.client.aclose()
    detail = await ok(await corg.c.get(f"{API}/deliverables/{d['id']}"))
    assert [e["kind"] for e in detail["events"]] == ["created", "started", "submitted", "rejected"]
    assert detail["events"][-1]["actor"]["kind"] == "client_user"
    # internal recording of a rejection
    await ok(await corg.c.post(f"{API}/deliverables/{d['id']}/submit", headers=corg.h()))
    rec = await ok(await corg.c.post(f"{API}/deliverables/{d['id']}/record-rejection", json={"reason": "Client called: redo"}, headers=corg.h()))
    assert rec["status"] == "in_progress" and rec["events"][-1]["actor"]["kind"] == "recorded_by_user"


async def test_portal_isolation_restricted_users_and_permissions(app: FastAPI, corg: COrg) -> None:
    _, _, prj = await _setup(corg)
    d = await deliverable(corg, prj["id"])
    await submitted(corg, d["id"])
    other_client = uuid.UUID((await corg.c.post(f"{API}/clients", json={"name": "Client B"}, headers=corg.h())).json()["id"])
    b_user = await client_user(app, corg, client_id=other_client)
    no_accept = await client_user(app, corg, perms=("projects:view",))
    no_view = await client_user(app, corg, perms=("invoices:view",))
    restricted = await client_user(app, corg, restricted=True)
    try:
        assert await ok(await b_user.client.get(f"{CP}/projects")) == []
        assert (await b_user.client.get(f"{CP}/projects/{prj['id']}")).status_code == 404
        assert (await b_user.client.post(f"{CP}/deliverables/{d['id']}/accept", headers=b_user.h())).status_code == 404
        err(await no_accept.client.post(f"{CP}/deliverables/{d['id']}/accept", headers=no_accept.h()), 403, "portal_permission_required")
        err(await no_view.client.get(f"{CP}/projects"), 403, "portal_permission_required")
        # restricted: nothing until one of their scoped placements is assigned to the project
        assert await ok(await restricted.client.get(f"{CP}/projects")) == []
        assert (await restricted.client.post(f"{CP}/deliverables/{d['id']}/accept", headers=restricted.h())).status_code == 404
        r = await role(corg, prj["id"])
        p1 = await placement(corg)
        await ok(await assign(corg, r["id"], p1), 201)
        from tests.modules.client_portal.helpers import scope_to

        await scope_to(corg.tenant_id, restricted.id, "placement", p1)
        listed = await ok(await restricted.client.get(f"{CP}/projects"))
        assert [p["id"] for p in listed] == [prj["id"]]
        detail = await ok(await restricted.client.get(f"{CP}/projects/{prj['id']}"))
        assert detail["roles"][0]["workers"] == ["Priya R."]
        blob = str(detail).lower()
        for stem in ("bill_rate", "cost", "margin", "budget", "burn", "vendor", "notes", "pay_rate"):
            assert stem not in blob, stem
    finally:
        for u in (b_user, no_accept, no_view, restricted):
            await u.client.aclose()
    # draft projects are not shown in the portal
    draft = await project(corg, activate=False, name="Draft project")
    cu = await client_user(app, corg)
    try:
        assert (await cu.client.get(f"{CP}/projects/{draft['id']}")).status_code == 404
    finally:
        await cu.client.aclose()


def test_portal_schemas_are_cost_free() -> None:
    from app.core import field_visibility as fv
    from app.schemas import projects_portal

    stems = ("bill_rate", "cost", "margin", "budget", "burn", "vendor", "pay_rate", "notes", "note")
    registered = fv.registered_schemas()
    for name in projects_portal.__all__:
        model = getattr(projects_portal, name)
        if model.model_config.get("extra") == "forbid":
            continue
        assert "client_user" in registered[model], name
        assert fv.violations(model, "client_user") == [], name
        for field in model.model_fields:
            assert not any(field.startswith(s) for s in stems), f"{name}.{field}"
