# ruff: noqa: E501
"""Story 37.2 (FR-188): baseline evidence, recruiter verification (human only, skill:verify), supersede, the
AssessmentScored / ProjectAssignmentEnded handlers (synthetic events, idempotent) and the evidence API scope."""

import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import emit
from app.events.assessments import AssessmentScored
from app.models.candidates import CandidateSkill
from app.models.projects import Project, ProjectRole
from app.models.skills_graph import CandidateSkillEvidence, SkillSuggestion
from app.services import skills_service
from tests.epic5 import h
from tests.modules.demands.helpers import DOrg, create_demand
from tests.modules.identity.conftest import join, new_client
from tests.modules.skills.helpers import CANDIDATES, candidate_skill_ids, candidate_with, gkey


def _ev_url(cid: str, csid: uuid.UUID) -> str:
    return f"{CANDIDATES}/{cid}/skills/{csid}/evidence"


async def _profile(m: Any, cid: str) -> dict[str, dict[str, Any]]:
    resp = await m.client.get(f"{CANDIDATES}/{cid}/skill-profile")
    assert resp.status_code == 200, resp.text
    return {r["name"]: r for r in resp.json()}


async def test_baseline_self_then_recruiter_verifies(dorg: DOrg) -> None:
    m = dorg.admin
    cand = await candidate_with(m, "Vera Verify", [{"name": "Java", "years": 5}])
    profile = await _profile(m, cand["id"])
    assert profile["Java"]["verification"] == "declared" and profile["Java"]["evidence_count"] == 1
    csid = (await candidate_skill_ids(dorg.tenant_id, cand["id"]))["Java"]
    r = await m.client.post(_ev_url(cand["id"], csid), json={"kind": "self", "proficiency": 5}, headers=h(m))
    assert r.status_code == 201, r.text
    assert r.json()["proficiency_after"] == 5 and r.json()["verification_after"] == "declared"
    r = await m.client.post(
        _ev_url(cand["id"], csid), json={"kind": "recruiter", "proficiency": 3}, headers=h(m)
    )
    assert (
        r.status_code == 201
        and r.json()["verification_after"] == "verified"
        and r.json()["proficiency_after"] == 3
    )
    r = await m.client.post(_ev_url(cand["id"], csid), json={"kind": "self", "proficiency": 5}, headers=h(m))
    assert r.json()["proficiency_after"] == 3 and r.json()["verification_after"] == "verified"
    # candidate skill list output reflects the recompute
    skills = (await m.client.get(f"{CANDIDATES}/{cand['id']}")).json()["skills"]
    java = next(s for s in skills if s["skill"]["name"] == "Java")
    assert java["proficiency"] == 3 and java["verification"] == "verified"
    history = (await m.client.get(_ev_url(cand["id"], csid))).json()
    assert [e["kind"] for e in history][:2] == ["self", "recruiter"] and len(history) == 4
    # supersede the recruiter evidence -> back to declared (latest self 5)
    rec = next(e for e in history if e["kind"] == "recruiter")
    sup = await m.client.post(f"{_ev_url(cand['id'], csid)}/{rec['id']}/supersede", headers=h(m))
    assert sup.status_code == 200 and sup.json()["verification_after"] == "declared"
    assert sup.json()["proficiency_after"] == 5
    # system kinds cannot be posted by a person
    bad = await m.client.post(
        _ev_url(cand["id"], csid), json={"kind": "assessment", "proficiency": 5}, headers=h(m)
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "evidence_kind_not_allowed"


async def test_api_key_recruiter_evidence_is_human_only(app: FastAPI, dorg: DOrg) -> None:
    m = dorg.admin
    cand = await candidate_with(m, "Kay Key", ["Python"])
    csid = (await candidate_skill_ids(dorg.tenant_id, cand["id"]))["Python"]
    created = await m.client.post(
        "/api/v1/api-keys",
        json={"name": "sync", "scopes": ["candidate:read", "candidate:update"]},
        headers=h(m),
    )
    assert created.status_code == 201, created.text
    async with new_client(app) as client:
        resp = await client.post(
            _ev_url(cand["id"], csid),
            json={"kind": "recruiter", "proficiency": 4},
            headers={"Authorization": f"Bearer {created.json()['key']}"},
        )
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "human_only_action"


async def test_skill_verify_permission_and_cross_tenant(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    cand = await candidate_with(dorg.admin, "Owen Owned", ["Python"])
    csid = (await candidate_skill_ids(dorg.tenant_id, cand["id"]))["Python"]
    viewer = await join(app, dorg.tenant, "viewer", name="Vic Viewer")
    try:
        r = await viewer.client.post(
            _ev_url(cand["id"], csid), json={"kind": "recruiter", "proficiency": 4}, headers=h(viewer)
        )
        assert r.status_code in (403, 404)
    finally:
        await viewer.client.aclose()
    o = other_dorg.admin
    assert (await o.client.get(_ev_url(cand["id"], csid))).status_code == 404
    assert (
        await o.client.post(_ev_url(cand["id"], csid), json={"kind": "self"}, headers=h(o))
    ).status_code == 404
    assert (await o.client.get(f"{CANDIDATES}/{cand['id']}/skill-profile")).status_code == 404


async def _emit_assessment(
    tid: uuid.UUID, cid: str, scores: tuple[tuple[str, int], ...], inv: uuid.UUID
) -> None:
    async with tenant_session(tid) as db:
        emit(
            db,
            AssessmentScored(
                tenant_id=tid,
                invitation_id=inv,
                assessment_id=uuid.uuid4(),
                candidate_id=uuid.UUID(cid),
                application_id=None,
                score="80",
                skill_scores=scores,
                source="builtin",
            ),
        )


async def test_assessment_scored_evidence_suggestion_and_idempotency(dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    cand = await candidate_with(m, "Asha Assessed", ["Java"])
    java, python = await gkey("Java", tid), await gkey("Python", tid)
    inv = uuid.uuid4()
    for _ in range(2):  # idempotent by source_ref
        await _emit_assessment(tid, cand["id"], ((java, 4), (python, 3)), inv)
    profile = await _profile(m, cand["id"])
    assert profile["Java"]["verification"] == "verified" and profile["Java"]["proficiency"] == 4
    async with tenant_session(tid) as db:
        n = len(
            list(
                (
                    await db.execute(
                        select(CandidateSkillEvidence).where(CandidateSkillEvidence.kind == "assessment")
                    )
                ).scalars()
            )
        )
        sugg = (
            await db.execute(select(SkillSuggestion).where(SkillSuggestion.source == "assessment"))
        ).scalar_one()
    assert n == 1 and sugg.proposed_proficiency == 3
    # accepting an assessment suggestion verifies (needs skill:verify; the admin has it)
    r = await m.client.post(
        f"{CANDIDATES}/{cand['id']}/skill-suggestions/{sugg.id}/accept", json={}, headers=h(m)
    )
    assert r.status_code == 200, r.text
    profile = await _profile(m, cand["id"])
    assert profile["Python"]["verification"] == "verified" and profile["Python"]["proficiency"] == 3


async def test_project_assignment_ended_uses_role_demand_skills(dorg: DOrg) -> None:
    m, tid = dorg.admin, dorg.tenant_id
    cand = await candidate_with(m, "Pria Project", ["Java"])
    demand = await create_demand(
        m,
        dorg.client_id,
        skills=[{"label": "Java", "kind": "mandatory", "min_proficiency": 3}, {"label": "Kotlin"}],
    )
    async with tenant_session(tid) as db:
        project = Project(
            id=uuid.uuid4(),
            tenant_id=tid,
            human_id=f"PRJ-{uuid.uuid4().hex[:6]}",
            name="Core",
            client_id=dorg.client_id,
        )
        db.add(project)
        await db.flush()
        role = ProjectRole(
            id=uuid.uuid4(), tenant_id=tid, project_id=project.id, client_id=dorg.client_id, title="Dev"
        )
        db.add(role)
        await db.flush()
        from app.models.demands import Demand

        d = await db.get(Demand, uuid.UUID(demand["id"]))
        assert d is not None
        d.project_role_id = role.id
        role_id = role.id

    class _E:
        assignment_id = uuid.uuid4()
        project_id = None
        project_role_id = role_id
        placement_id = None
        candidate_id = uuid.UUID(cand["id"])
        ended_on = None

    for _ in range(2):
        async with tenant_session(tid) as db:
            out = await skills_service.on_project_assignment_ended(
                RequestContext(tenant_id=tid, actor_kind="system"), db, _E()
            )
    assert out == {"evidence": 0, "suggestions": 0}  # second run: idempotent
    async with tenant_session(tid) as db:
        ev = list(
            (
                await db.execute(
                    select(CandidateSkillEvidence).where(CandidateSkillEvidence.kind == "project")
                )
            ).scalars()
        )
        cs = (
            await db.execute(select(CandidateSkill).where(CandidateSkill.id == ev[0].candidate_skill_id))
        ).scalar_one()
        sugg = list(
            (await db.execute(select(SkillSuggestion).where(SkillSuggestion.source == "project"))).scalars()
        )
    assert len(ev) == 1 and ev[0].proficiency == 3
    assert cs.verification == "declared"  # project evidence is inferred class: never upgrades to verified
    kotlin = await gkey("Kotlin", tid)
    assert kotlin in {f"global:{x.skill_id}" for x in sugg}  # a suggestion, not a skill
    assert "Kotlin" not in await candidate_skill_ids(tid, cand["id"])
