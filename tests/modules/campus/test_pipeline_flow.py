# ruff: noqa: E501
"""Epic 43 stories 43.4 / 43.6: bulk assessment invite, shortlist propose / confirm (star), interview batches with
auto-slotting into P1 interviews, bulk offers through ``offers_service.create_bulk``, joining / reneging and the
funnel by Institution."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.core.events import emit
from app.events.assessments import AssessmentScored
from app.schemas.campus import ConfirmIn
from app.services import offers_service
from app.services.campus import shortlist
from tests.modules.assessments.helpers import simple_assessment
from tests.modules.campus.helpers import (
    CAMPUS,
    answers,
    ctx,
    ready_drive,
    register,
    registrations,
    rows,
    scalar,
    token,
)
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import new_client
from tests.modules.offers.helpers import move, release, stage_key, submit

SCORES = {"s1": "90.00", "s2": "80.00", "s3": "80.00", "s4": "70.00", "s5": "60.00"}


async def _score(org: DOrg, invitation_id: str, score: str, candidate_id: str, assessment_id: str) -> None:
    async with tenant_session(org.tenant_id) as db:
        emit(
            db,
            AssessmentScored(
                tenant_id=org.tenant_id, invitation_id=uuid.UUID(invitation_id), assessment_id=uuid.UUID(assessment_id),
                candidate_id=uuid.UUID(candidate_id), application_id=None, score=score, skill_scores=(), source="builtin",
            ),
        )


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    client = new_client(app)
    yield client
    await client.aclose()


async def test_drive_pipeline_end_to_end(dorg: DOrg, anon: httpx.AsyncClient) -> None:
    assessment, _ = await simple_assessment(dorg)
    drive = await ready_drive(dorg, assessment_id=assessment["id"])
    tok = token(drive["institutions"][0]["registration_url"])
    for key in SCORES:
        r = await register(anon, tok, email=f"{key}@college.example.edu", name=f"Student {key.upper()}", ans=answers())
        assert r.status_code == 200, r.text
    deadline = (datetime.now(UTC) + timedelta(days=5)).isoformat()

    # ---- bulk invite (idempotent)
    inv = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/assessments/invite", json={"deadline_at": deadline}, headers=h(dorg.admin))
    assert inv.status_code == 200, inv.text
    assert inv.json()["invited"] == 5 and inv.json()["batches"] == 1 and inv.json()["errors"] == []
    again = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/assessments/invite", json={"deadline_at": deadline}, headers=h(dorg.admin))
    assert again.json()["invited"] == 0 and again.json()["skipped"] == 5
    assert await scalar(dorg, "SELECT count(*) FROM assessment_invitations WHERE context_kind = 'campus_drive' AND context_id = :d", d=drive["id"]) == 5
    assert (await dorg.admin.client.get(f"{CAMPUS}/drives/{drive['id']}")).json()["status"] == "in_progress"

    regs = {r["candidate_email"].split("@")[0]: r for r in await registrations(dorg, drive["id"])}
    for key, score in SCORES.items():
        reg = regs[key]
        await _score(dorg, reg["assessment_invitation_id"], score, reg["candidate_id"], assessment["id"])
    regs = {r["candidate_email"].split("@")[0]: r for r in await registrations(dorg, drive["id"])}
    assert all(r["status"] == "assessed" for r in regs.values())
    assert regs["s1"]["score"] == "90.00"

    # ---- proposal changes nothing; ties at the cut-off are included and flagged
    p = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/shortlist/propose", json={"mode": "top_n", "value": "2"}, headers=h(dorg.admin))
    assert p.status_code == 200, p.text
    prop = p.json()
    assert prop["count"] == 3 and prop["ties"] == 2 and prop["cutoff"] == "80.00"
    assert all(r["status"] == "assessed" for r in await registrations(dorg, drive["id"]))

    # ---- confirm is human only
    demand_id = drive["demands"][0]["id"]
    ids = [i["registration_id"] for i in prop["items"]]
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Forbidden):
            await shortlist.confirm(ctx(dorg, actor_kind="workflow"), db, uuid.UUID(drive["id"]), ConfirmIn(registration_ids=[uuid.UUID(i) for i in ids], demand_id=uuid.UUID(demand_id)))
    c = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/shortlist/confirm", json={"registration_ids": [*ids, str(uuid.uuid4())], "demand_id": demand_id}, headers=h(dorg.admin))
    assert c.status_code == 200, c.text
    assert c.json()["shortlisted"] == 3
    assert c.json()["results"][-1]["error"] == "not_found"
    apps = await rows(dorg, "SELECT source, source_ref FROM applications WHERE demand_id = :d", d=demand_id)
    assert len(apps) == 3 and {a[0] for a in apps} == {"campus"} and {a[1] for a in apps} == set(ids)

    # ---- interview batch: 2 panels x 2 slots, auto-slot round-robin -> P1 interviews
    day = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    panels = [{"label": "Panel A", "interviewer_user_ids": [str(dorg.admin.user_id)], "room": "Room 101"}, {"label": "Panel B", "interviewer_user_ids": [str(dorg.admin.user_id)], "video_link": "https://meet.example.com/b"}]
    b = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/batches", json={"demand_id": demand_id, "date": day, "start_time": "10:00", "end_time": "11:00", "timezone": "Asia/Kolkata", "slot_minutes": 30, "panels": panels}, headers=h(dorg.admin))
    assert b.status_code == 201, b.text
    assert b.json()["slot_count"] == 4
    a = await dorg.admin.client.post(f"{CAMPUS}/batches/{b.json()['id']}/auto-slot", headers=h(dorg.admin))
    assert a.status_code == 200, a.text
    assert a.json()["slotted"] == 3 and a.json()["errors"] == [], a.text
    slots = sorted((s for s in a.json()["batch"]["slots"] if s["registration_id"]), key=lambda s: (s["starts_at"], s["panel_index"]))
    assert [s["panel_index"] for s in slots] == [0, 1, 0]
    assert slots[0]["registration_id"] == regs["s1"]["id"]
    assert await scalar(dorg, "SELECT count(*) FROM interviews WHERE demand_id = :d AND status = 'scheduled'", d=demand_id) == 3
    first_app = uuid.UUID(regs["s1"]["application_id"] or (await registrations(dorg, drive["id"]))[0]["application_id"])
    assert await stage_key(dorg, first_app) == "interviewing"
    free = next(s for s in a.json()["batch"]["slots"] if s["registration_id"] is None)
    before = await scalar(dorg, "SELECT starts_at FROM interviews WHERE id = :i", i=slots[0]["interview_id"])
    mv = await dorg.admin.client.post(f"{CAMPUS}/batches/{b.json()['id']}/slots/{slots[0]['id']}/move", json={"to_slot_id": free["id"]}, headers=h(dorg.admin))
    assert mv.status_code == 200, mv.text
    after = await scalar(dorg, "SELECT starts_at FROM interviews WHERE id = :i", i=slots[0]["interview_id"])
    assert after != before

    # ---- interviewed via selection, bulk offers (per item, each with its own approval)
    regs = {r["candidate_email"].split("@")[0]: r for r in await registrations(dorg, drive["id"])}
    shortlisted = [r for r in regs.values() if r["status"] == "shortlisted"]
    assert len(shortlisted) == 3
    picked = sorted(shortlisted, key=lambda r: r["score"], reverse=True)
    for r in picked[:2]:
        await move(dorg, uuid.UUID(r["application_id"]), ("selected", "user"))
    regs = {r["id"]: r for r in await registrations(dorg, drive["id"])}
    assert [regs[r["id"]]["status"] for r in picked] == ["interviewed", "interviewed", "shortlisted"]
    joining = (date.today() + timedelta(days=60)).isoformat()
    o = await dorg.admin.client.post(f"{CAMPUS}/drives/{drive['id']}/offers", json={"registration_ids": [r["id"] for r in picked], "planned_joining_date": joining}, headers=h(dorg.admin))
    assert o.status_code == 200, o.text
    out = o.json()
    assert out["created"] == 2 and out["failed"] == 1
    assert out["results"][2]["error_code"] == "not_interviewed"
    offers = [x["offer_id"] for x in out["results"][:2]]
    starts = await rows(dorg, "SELECT v.start_date FROM offers o JOIN offer_versions v ON v.id = o.current_version_id WHERE o.id = ANY(:ids)", ids=[uuid.UUID(i) for i in offers])
    assert {s[0].isoformat() for s in starts} == {joining}
    regs = {r["id"]: r for r in await registrations(dorg, drive["id"])}
    assert regs[picked[0]["id"]]["status"] == "offered"

    # ---- accept -> placement; renege after acceptance -> P2 dropout; renege on an open offer -> withdraw
    for oid in offers:
        await submit(dorg.admin, oid)
        await release(dorg.admin, oid)
    async with tenant_session(dorg.tenant_id) as db:
        await offers_service.CandidateResponder(db, uuid.UUID(picked[0]["candidate_id"]), "portal", "203.0.113.4", "pytest").accept(uuid.UUID(offers[0]), picked[0]["candidate_name"], 1, True)
    regs = {r["id"]: r for r in await registrations(dorg, drive["id"])}
    assert regs[picked[0]["id"]]["accepted_at"] is not None
    pstatus = await scalar(dorg, "SELECT status FROM placements WHERE offer_id = :o", o=uuid.UUID(offers[0]))
    assert pstatus in ("pre_joining", "joining_confirmed")
    rn = await dorg.admin.client.post(f"{CAMPUS}/registrations/{picked[0]['id']}/renege", json={"note": "Joined higher studies"}, headers=h(dorg.admin))
    assert rn.status_code == 200 and rn.json()["status"] == "reneged", rn.text
    assert await scalar(dorg, "SELECT status FROM placements WHERE offer_id = :o", o=uuid.UUID(offers[0])) == "dropped"
    rn2 = await dorg.admin.client.post(f"{CAMPUS}/registrations/{picked[1]['id']}/renege", json={"note": "Not reachable"}, headers=h(dorg.admin))
    assert rn2.status_code == 200, rn2.text
    assert await scalar(dorg, "SELECT status FROM offers WHERE id = :o", o=uuid.UUID(offers[1])) == "withdrawn"
    assert (await scalar(dorg, "SELECT withdrawn_reason FROM offers WHERE id = :o", o=uuid.UUID(offers[1]))).startswith("Reneged:")

    # ---- joining tracking + funnel by Institution
    jt = (await dorg.admin.client.get(f"{CAMPUS}/drives/{drive['id']}/offers")).json()
    assert len(jt) == 2 and {j["status"] for j in jt} == {"reneged"}
    f = (await dorg.admin.client.get(f"{CAMPUS}/drives/{drive['id']}/funnel")).json()
    row = f["rows"][0]
    assert (row["registered"], row["eligible"], row["assessed"], row["shortlisted"], row["interviewed"], row["offered"], row["accepted"], row["reneged"]) == (5, 5, 5, 3, 2, 2, 1, 2)
    assert f["stages"][-1] == "joined"
    reasons = (await dorg.admin.client.get(f"{CAMPUS}/offer-withdrawal-reasons")).json()
    assert reasons[0] == "Reneged"
