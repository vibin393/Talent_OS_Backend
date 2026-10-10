"""Epic 18 FR-100/FR-101: demands, demand requests, sent submissions, compare, feedback, slots (Client Portal)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.client_portal import InterviewSlotProposal
from app.models.demands import Demand
from app.models.interviews import Interview
from app.models.pipeline import Submission
from app.models.tasks import Task
from tests.modules.client_portal.helpers import (
    CP,
    clean,
    client_user,
    other_client,
    scope_to,
    sent_submission,
)
from tests.modules.pipeline.helpers import POrg, h


def _slot(days: int, hours: int = 1) -> dict[str, str]:
    start = datetime.now(UTC) + timedelta(days=days)
    return {
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=hours)).isoformat(),
        "tz": "Asia/Kolkata",
    }


async def test_sent_submissions_compare_feedback_and_fr80(app: FastAPI, porg: POrg) -> None:
    _, sub1 = await sent_submission(porg, "Priya Raman")
    _, sub2 = await sent_submission(porg, "Arun Kumar")
    cu = await client_user(app, porg.tenant_id, porg.client_id)

    demands = (await cu.client.get(f"{CP}/demands")).json()
    assert [d["id"] for d in demands] == [porg.demand_id]
    assert demands[0]["sent_submissions"] == 2

    resp = await cu.client.get(f"{CP}/submissions", params={"demand_id": porg.demand_id})
    assert resp.status_code == 200, resp.text
    subs = resp.json()
    assert {s["id"] for s in subs} == {sub1, sub2}
    clean(subs)
    assert all(s["candidate_display_name"].endswith(".") for s in subs)
    assert subs[0]["bill_rate"] == "1500.00"

    # compare 2..4 from one demand
    ok = await cu.client.post(
        f"{CP}/submissions/compare", json={"submission_ids": [sub1, sub2]}, headers=cu.h()
    )
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["items"]) == 2
    one = await cu.client.post(f"{CP}/submissions/compare", json={"submission_ids": [sub1]}, headers=cu.h())
    assert one.status_code == 422 and one.json()["error"]["code"] == "compare_limit"
    five = [sub1, sub2, *[str(uuid.uuid4()) for _ in range(3)]]
    too_many = await cu.client.post(
        f"{CP}/submissions/compare", json={"submission_ids": five}, headers=cu.h()
    )
    assert too_many.status_code == 422 and too_many.json()["error"]["code"] == "compare_limit"

    # resume of the sent copy
    res = await cu.client.get(f"{CP}/submissions/{sub1}/resume")
    assert res.status_code == 200 and res.content[:4] == b"%PDF"

    # feedback: interested, then rejected stays rejected_pending
    fb = await cu.client.post(
        f"{CP}/submissions/{sub1}/feedback", json={"decision": "interested"}, headers=cu.h()
    )
    assert fb.status_code == 200, fb.text
    assert fb.json()["feedback_status"] == "interested"
    rj = await cu.client.post(
        f"{CP}/submissions/{sub2}/feedback",
        json={"decision": "rejected", "comment": "Not enough Spark"},
        headers=cu.h(),
    )
    assert rj.json()["feedback_status"] == "rejected_pending"
    async with tenant_session(porg.tenant_id) as db:
        sources = (await db.execute(text("SELECT source FROM client_feedback"))).scalars().all()
    assert set(sources) == {"portal"}


async def test_isolation_unsent_other_client_restricted(app: FastAPI, porg: POrg, other_porg: POrg) -> None:
    _, sub = await sent_submission(porg)
    other_id = await other_client(porg)
    stranger = await client_user(app, porg.tenant_id, other_id)
    assert (await stranger.client.get(f"{CP}/submissions/{sub}")).status_code == 404
    assert (await stranger.client.get(f"{CP}/demands/{porg.demand_id}")).status_code == 404
    assert (await stranger.client.get(f"{CP}/submissions")).json() == []

    tenant2 = await client_user(app, other_porg.tenant_id, other_porg.client_id)
    assert (await tenant2.client.get(f"{CP}/submissions/{sub}")).status_code == 404

    restricted = await client_user(app, porg.tenant_id, porg.client_id, restricted=True)
    assert (await restricted.client.get(f"{CP}/submissions/{sub}")).status_code == 404
    await scope_to(porg.tenant_id, restricted.id, "demand", uuid.UUID(porg.demand_id))
    assert (await restricted.client.get(f"{CP}/submissions/{sub}")).status_code == 200

    # an unsent submission is invisible
    async with tenant_session(porg.tenant_id) as db:
        await db.execute(text("UPDATE submissions SET status = 'cancelled' WHERE id = :i"), {"i": sub})
    me = await client_user(app, porg.tenant_id, porg.client_id)
    assert (await me.client.get(f"{CP}/submissions/{sub}")).status_code == 404

    # tos_client never passes internal routes
    assert (await me.client.get("/api/v1/demands")).status_code == 401
    assert (await me.client.get(f"/api/v1/clients/{porg.client_id}/compliance-packs")).status_code == 401


async def test_demand_request_is_a_draft_for_the_account_manager(app: FastAPI, porg: POrg) -> None:
    cu = await client_user(app, porg.tenant_id, porg.client_id, permissions=())
    body = {"title": "Platform Engineer", "positions": 2, "skills": ["Kubernetes"], "location": "Pune"}
    resp = await cu.client.post(f"{CP}/demand-requests", data={"payload": json.dumps(body)}, headers=cu.h())
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "draft" and out["source"] == "client_portal"
    async with tenant_session(porg.tenant_id) as db:
        d = await db.get(Demand, uuid.UUID(out["id"]))
        assert d is not None and d.status == "draft" and d.owner_id == porg.admin.user_id
        req = (
            await db.execute(
                text("SELECT requested_by_client_user_id FROM demands WHERE id = :i"), {"i": out["id"]}
            )
        ).scalar_one()
        assert req == cu.id
    mine = (await cu.client.get(f"{CP}/demands")).json()
    assert any(m["id"] == out["id"] and m["requested_by_me"] for m in mine)
    # a draft not requested through the portal stays invisible
    async with tenant_session(porg.tenant_id) as db:
        await db.execute(text("UPDATE demands SET status = 'draft' WHERE id = :i"), {"i": porg.demand_id})
    assert (await cu.client.get(f"{CP}/demands/{porg.demand_id}")).status_code == 404


async def test_slots_create_a_task_and_no_interview(app: FastAPI, porg: POrg) -> None:
    app_id, sub = await sent_submission(porg)
    cu = await client_user(app, porg.tenant_id, porg.client_id)
    bad = await cu.client.post(
        f"{CP}/submissions/{sub}/slot-proposals", json={"slots": [_slot(-1)]}, headers=cu.h()
    )
    assert bad.status_code == 422
    six = await cu.client.post(
        f"{CP}/submissions/{sub}/slot-proposals",
        json={"slots": [_slot(i + 1) for i in range(6)]},
        headers=cu.h(),
    )
    assert six.status_code == 422
    resp = await cu.client.post(
        f"{CP}/submissions/{sub}/slot-proposals", json={"slots": [_slot(2), _slot(3)]}, headers=cu.h()
    )
    assert resp.status_code == 201, resp.text
    async with tenant_session(porg.tenant_id) as db:
        tasks = (await db.execute(select(Task).where(Task.entity_id == uuid.UUID(app_id)))).scalars().all()
        assert any(t.source == "client_portal" for t in tasks)
        assert (
            await db.execute(select(Interview).where(Interview.application_id == uuid.UUID(app_id)))
        ).first() is None

    # recruiter offers slots, client chooses one -> Task, still no interview
    offer = await porg.admin.client.post(
        f"/api/v1/applications/{app_id}/slot-offers",
        json={"slots": [_slot(4), _slot(5)]},
        headers=h(porg.admin),
    )
    assert offer.status_code == 201, offer.text
    chosen = await cu.client.post(
        f"{CP}/slot-offers/{offer.json()['id']}/choose", json={"index": 1}, headers=cu.h()
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["status"] == "chosen" and chosen.json()["chosen_index"] == 1
    again = await cu.client.post(
        f"{CP}/slot-offers/{offer.json()['id']}/choose", json={"index": 0}, headers=cu.h()
    )
    assert again.status_code == 409
    async with tenant_session(porg.tenant_id) as db:
        rows = (await db.execute(select(InterviewSlotProposal))).scalars().all()
        assert len(rows) == 2
        assert (await db.execute(select(Interview))).first() is None


async def test_org_rls_hides_hiring_rows_without_org_context(porg: POrg) -> None:
    await sent_submission(porg)
    from app.core.database import TENANT_INFO_KEY, _unit_of_work

    async with _unit_of_work({TENANT_INFO_KEY: str(porg.tenant_id)}) as db:
        for table in (
            "demands",
            "submissions",
            "client_feedback",
            "interviews",
            "interview_participants",
            "scorecards",
            "interview_slot_proposals",
            "client_user_delegations",
            "client_compliance_packs",
        ):
            assert (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0, table
    async with tenant_session(porg.tenant_id) as db:
        assert (await db.execute(select(Submission))).first() is not None
