# ruff: noqa: E501
"""Epic 40 A9-A13: provider (fake / Manual), Epic 37 evidence, stage plan, workflow auto-invite, Submission shares,
semantic source, erasure, Candidate Portal isolation."""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.semantic import sources
from app.models.assessments import Attempt, AssessmentInvitation
from app.models.connectors import ConnectorJob
from app.services.connectors.assessment.fake import FakeAssessmentProvider
from app.services.privacy.registry import ErasureContext, handlers
from tests.modules.assessments.helpers import (
    API,
    ASM,
    INV,
    answer_for,
    create_assessment,
    invite_ok,
    link_token,
    make_candidate,
    save,
    simple_assessment,
    sql,
    start,
    submit,
)
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.offers.helpers import add_application
from tests.p2 import sign_in_candidate

SECRET = "assessment-hook-secret"


async def _fake_config(org: DOrg) -> str:
    body = {"category": "assessment", "provider_key": "fake", "display_name": "Fake tests", "webhook_secret": SECRET}
    r = await org.admin.client.post(f"{API}/connectors/configs", json=body, headers=h(org.admin))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _python_key(org: DOrg) -> str:
    from tests.modules.skills.helpers import gkey

    return await gkey("Python", org.tenant_id)


async def _evidence(org: DOrg, candidate_id: uuid.UUID) -> list[Any]:
    return await sql(
        org,
        "SELECT e.kind, e.proficiency FROM candidate_skill_evidence e JOIN candidate_skills s ON s.id = e.candidate_skill_id WHERE s.candidate_id = :c AND e.kind = 'assessment'",
        c=candidate_id,
    )


async def test_fake_provider_invite_webhook_import_and_minimal_payload(dorg: DOrg) -> None:
    FakeAssessmentProvider.reset()
    c = dorg.admin.client
    config_id = await _fake_config(dorg)
    tests = await c.post(f"{ASM}/provider-catalogue/sync", json={"config_id": config_id}, headers=h(dorg.admin))
    assert tests.status_code == 200 and {t["external_ref"] for t in tests.json()} == {"fake-python-101", "fake-sql-201"}
    a = await create_assessment(dorg, name="Executed coding", delivery="provider", provider_config_id=config_id, provider_test_ref="fake-python-101")
    assert (await c.post(f"{ASM}/{a['id']}/publish", headers=h(dorg.admin))).status_code == 200
    cand = await make_candidate(dorg, "Pia Provider", skills=["Python"])
    inv = await invite_ok(dorg, a["id"], cand)
    async with tenant_session(dorg.tenant_id) as db:
        job = (await db.execute(select(ConnectorJob).where(ConnectorJob.subject_id == uuid.UUID(inv["id"])))).scalar_one()
        assert job.idempotency_key == f"assessment:invite:assessment_invitation:{inv['id']}:1"
        assert set(job.payload) == {"tenant_id", "invitation_id", "external_test_id", "candidate_email", "candidate_name", "deadline_at", "callback_url"}
        assert job.payload["candidate_name"] == "Pia Provider"
    detail = (await c.get(f"{INV}/{inv['id']}")).json()
    assert detail["provider_ref"] == f"fake-asmt:fake-python-101:{inv['id']}"
    # Unmapped first: the webhook imports the score, the skill stays display-only, no evidence.
    body = json.dumps({"external_ref": detail["provider_ref"]}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    hook = await c.post(f"{API}/hooks/assessment/{config_id}", content=body, headers={"X-Fake-Signature": sig, "Content-Type": "application/json"})
    assert hook.status_code == 202, hook.text
    res = (await c.get(f"{INV}/{inv['id']}")).json()
    assert res["status"] == "reviewed" and res["score"] == "82.50"
    assert res["report_url"].startswith("https://assessments.fake.test/report/")
    assert list(res["skill_scores"]) == ["provider:python"] and res["skill_scores"]["provider:python"]["proficiency"] is None
    assert await _evidence(dorg, cand) == []
    bad = await c.post(f"{API}/hooks/assessment/{config_id}", content=body, headers={"X-Fake-Signature": "nope"})
    assert bad.status_code == 401
    # Mapped skill on a second candidate -> AssessmentScored carries (key, 4) -> Epic 37 evidence.
    key = await _python_key(dorg)
    m = await c.put(f"{ASM}/skill-map", json={"config_id": config_id, "rows": [{"provider_skill": "Python", "skill_key": key}]}, headers=h(dorg.admin))
    assert m.status_code == 200, m.text
    cand2 = await make_candidate(dorg, "Raj Mapped", skills=["Python"])
    inv2 = await invite_ok(dorg, a["id"], cand2)
    ref2 = (await c.get(f"{INV}/{inv2['id']}")).json()["provider_ref"]
    body2 = json.dumps({"external_ref": ref2}).encode()
    await c.post(f"{API}/hooks/assessment/{config_id}", content=body2, headers={"X-Fake-Signature": hmac.new(SECRET.encode(), body2, hashlib.sha256).hexdigest()})
    res2 = (await c.get(f"{INV}/{inv2['id']}")).json()
    assert res2["skill_scores"][key]["proficiency"] == 4
    assert await _evidence(dorg, cand2) == [("assessment", 4)]
    # star: Pass / Fail is still a person's decision
    ok = await c.post(f"{INV}/{inv2['id']}/decide", json={"result": "fail"}, headers=h(dorg.admin))
    assert ok.status_code == 200 and ok.json()["status"] == "failed"


async def test_manual_provider_result(dorg: DOrg) -> None:
    c = dorg.admin.client
    r = await c.post(f"{API}/connectors/configs", json={"category": "assessment", "provider_key": "manual", "display_name": "Manual"}, headers=h(dorg.admin))
    assert r.status_code == 201, r.text
    a = await create_assessment(dorg, name="Offline test", delivery="provider", provider_config_id=r.json()["id"], provider_test_ref="paper-1")
    await c.post(f"{ASM}/{a['id']}/publish", headers=h(dorg.admin))
    cand = await make_candidate(dorg, "Mo Manual", skills=["Python"])
    inv = await invite_ok(dorg, a["id"], cand)
    async with tenant_session(dorg.tenant_id) as db:
        job = (await db.execute(select(ConnectorJob).where(ConnectorJob.subject_id == uuid.UUID(inv["id"])))).scalar_one()
        assert job.status == "succeeded" and job.result.get("manual") is True
    key = await _python_key(dorg)
    rec = await c.post(f"{INV}/{inv['id']}/manual-result", json={"score_pct": "71.5", "result_note": "paper", "skill_scores": [{"skill_key": key, "proficiency": 3}]}, headers=h(dorg.admin))
    assert rec.status_code == 200, rec.text
    assert rec.json()["status"] == "reviewed" and rec.json()["score"] == "71.50"
    assert await _evidence(dorg, cand) == [("assessment", 3)]
    again = await c.post(f"{INV}/{inv['id']}/manual-result", json={"score_pct": "10"}, headers=h(dorg.admin))
    assert again.status_code == 409


async def test_builtin_result_creates_skill_evidence(dorg: DOrg) -> None:
    key = await _python_key(dorg)
    a, _ = await simple_assessment(dorg, ("single", "code_output"))
    from tests.modules.assessments.helpers import ITEMS

    items = (await dorg.admin.client.get(ITEMS)).json()
    for it in items:  # tag both items with the Python skill (new versions; the published version is frozen)
        body = {k: it["version"][k] for k in ("stem", "language", "code", "options", "points", "answer_key", "rubric")}
        await dorg.admin.client.put(f"{ITEMS}/{it['id']}", json={**body, "skill_keys": [key], "tags": [], "difficulty": 3}, headers=h(dorg.admin))
    a2, _ = await simple_assessment(dorg, ("single",))
    del a2
    tagged = (await dorg.admin.client.get(ITEMS)).json()
    ids = [t["id"] for t in tagged if t["skill_keys"] == [key]]
    from tests.modules.assessments.helpers import published

    pub = await published(dorg, [{"key": "py", "title": "Python", "items": [{"item_id": i} for i in ids]}], name="Python skills")
    cand = await make_candidate(dorg, "Eve Evidence", skills=["Python"])
    inv = await invite_ok(dorg, pub["id"], cand)
    token = await link_token(dorg, cand)
    c = dorg.admin.client
    view = await start(c, token)
    answers = [{"position": it["position"], "response": answer_for(it, it["kind"], correct=it["kind"] != "code_output")} for it in view["items"]]
    await save(c, token, view["session_id"], 1, answers)
    await submit(c, token, view["session_id"])
    res = (await c.get(f"{INV}/{inv['id']}")).json()
    assert res["status"] == "reviewed" and res["skill_scores"][key]["pct"] == "50.00" and res["skill_scores"][key]["proficiency"] == 2
    assert await _evidence(dorg, cand) == [("assessment", 2)]
    del a


async def test_stage_plan_validation_schedule_guard_and_workflow_auto_invite(dorg: DOrg) -> None:
    c = dorg.admin.client
    a, _ = await simple_assessment(dorg, ("single",))
    draft = await create_assessment(dorg, name="Unpublished")
    demand = await open_demand(dorg.admin, dorg.client_id)
    plan_url = f"{API}/demands/{demand['id']}/interview-plan"
    plan = (await c.get(plan_url)).json()
    stages = [{"id": s["id"], "name": s["name"], "kind": s["kind"]} for s in plan["stages"]]
    missing = await c.put(plan_url, json={"stages": [*stages, {"name": "Online test", "kind": "assessment"}]}, headers=h(dorg.admin))
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "stage_assessment_required"
    unpublished = await c.put(plan_url, json={"stages": [*stages, {"name": "Online test", "kind": "assessment", "assessment_id": draft["id"]}]}, headers=h(dorg.admin))
    assert unpublished.status_code == 422
    ok = await c.put(plan_url, json={"stages": [{"name": "Online test", "kind": "assessment", "assessment_id": a["id"]}, *stages]}, headers=h(dorg.admin))
    assert ok.status_code == 200, ok.text
    stage = ok.json()["stages"][0]
    assert stage["kind"] == "assessment" and stage["assessment_id"] == a["id"]
    # scheduling an interview in an assessment stage -> 409 (service-level guard)
    from app.services.assessments import stage_type
    from app.core.errors import DomainError
    import pytest

    async with tenant_session(dorg.tenant_id) as db:
        from app.models.interviews import InterviewStage

        row = await db.get(InterviewStage, uuid.UUID(stage["id"]))
        with pytest.raises(DomainError) as exc:
            await stage_type.schedule_guard(db, row)
        assert exc.value.code == "stage_is_assessment"
    # workflow rule: application stage changed -> invite (actor workflow)
    rule = {
        "name": "Assessment on shortlist",
        "trigger": "application.stage_changed",
        "actions": [{"type": "assessments.invite", "params": {"assessment_id": a["id"], "deadline_days": 3}}],
    }
    r = await c.post(f"{API}/workflow/rules", json=rule, headers=h(dorg.admin))
    assert r.status_code == 201, r.text
    assert (await c.post(f"{API}/workflow/rules/{r.json()['id']}/activate", headers=h(dorg.admin))).status_code == 200
    cand = await make_candidate(dorg, "Wes Workflow")
    app_id = await add_application(dorg.admin, uuid.UUID(demand["id"]), cand)
    from tests.modules.offers.helpers import move

    await move(dorg, app_id, ("shortlisted", "user"))
    rows = await sql(dorg, "SELECT invited_by_kind, context_kind, application_id FROM assessment_invitations WHERE candidate_id = :c", c=cand)
    assert rows and rows[0][0] == "workflow" and rows[0][1] == "application" and rows[0][2] == app_id
    badges = (await c.get(f"{API}/applications/assessment-badges", params={"ids": str(app_id)})).json()
    assert badges["items"][0]["status"] == "invited"


async def test_submission_shares_only_on_choice(dorg: DOrg) -> None:
    from app.services.assessments import shares as shares_mod

    c = dorg.admin.client
    a, _ = await simple_assessment(dorg, ("single",))
    cand = await make_candidate(dorg, "Sam Shared")
    inv = await invite_ok(dorg, a["id"], cand)
    token = await link_token(dorg, cand)
    view = await start(c, token)
    it = view["items"][0]
    await save(c, token, view["session_id"], 1, [{"position": it["position"], "response": answer_for(it, "single")}])
    await submit(c, token, view["session_id"])
    demand = await open_demand(dorg.admin, dorg.client_id)
    app_id = await add_application(dorg.admin, uuid.UUID(demand["id"]), cand)
    async with tenant_session(dorg.tenant_id) as db:
        from app.models.pipeline import Submission

        sub = Submission(id=uuid.uuid4(), tenant_id=dorg.tenant_id, human_id=f"SUB-{uuid.uuid4().hex[:6]}", application_id=app_id, demand_id=uuid.UUID(demand["id"]), candidate_id=cand, client_id=dorg.client_id, status="approved",
            pay_rate=Decimal("100"), bill_rate=Decimal("150"), currency="INR", rate_unit="hour", burden_pct=Decimal("0"),
            margin_amount=Decimal("50"), margin_pct=Decimal("33.33"), resume_kind="original",
        )
        db.add(sub)
        await db.flush()
        sub_id = sub.id
    async with tenant_session(dorg.tenant_id) as db:
        sub = await db.get(Submission, sub_id)
        assert await shares_mod.submission_attachments(db, sub) == []  # unshared never reaches the Client
    other = await make_candidate(dorg, "Not Theirs")
    a2, _ = await simple_assessment(dorg, ("single",))
    foreign = await invite_ok(dorg, a2["id"], other)
    bad = await c.put(f"{API}/submissions/{sub_id}/assessment-shares", json={"invitation_ids": [foreign["id"]]}, headers=h(dorg.admin))
    assert bad.status_code in (403, 404, 422), bad.text
    ok = await c.put(f"{API}/submissions/{sub_id}/assessment-shares", json={"invitation_ids": [inv["id"]]}, headers=h(dorg.admin))
    assert ok.status_code == 200, ok.text
    async with tenant_session(dorg.tenant_id) as db:
        files = await shares_mod.submission_attachments(db, await db.get(Submission, sub_id))
    assert len(files) == 1 and files[0].data.startswith(b"%PDF")
    md = shares_mod.summary_markdown("X", (await _inv(dorg, inv["id"])))
    assert "tab_switch" not in md and "Paris" not in md


async def _inv(org: DOrg, inv_id: str) -> AssessmentInvitation:
    async with tenant_session(org.tenant_id) as db:
        row = await db.get(AssessmentInvitation, uuid.UUID(inv_id))
        assert row is not None
        return row


def test_semantic_source_registered() -> None:
    from app.core.hooks import autodiscover

    autodiscover("app.services")
    src = sources()["assessments.result"]
    assert {"status", "score_pct", "percentile", "assessment.name", "candidate.full_name", "demand_id", "gating"} <= set(src.fields)
    assert all(f.permission == "assessment:read" for f in src.fields.values())
    assert src.fields["candidate.full_name"].pii_class == "personal"


async def test_erasure_deletes_attempts_and_anonymises_result(dorg: DOrg) -> None:
    c = dorg.admin.client
    a, _ = await simple_assessment(dorg, ("single",))
    cand = await make_candidate(dorg, "Ezra Erased")
    inv = await invite_ok(dorg, a["id"], cand)
    token = await link_token(dorg, cand)
    view = await start(c, token)
    it = view["items"][0]
    await save(c, token, view["session_id"], 1, [{"position": it["position"], "response": answer_for(it, "single")}])
    await c.post(f"{API}/public/links/{token}/assessment/attempt/signals", json={"signals": [{"kind": "copy"}]}, headers={"X-Attempt-Session": view["session_id"]})
    await submit(c, token, view["session_id"])
    handler = next(x for x in handlers() if x.name == "assessments.attempts")
    ectx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cand, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ectx)
        assert exported and exported["assessments"][0]["answers"][0]["response"]
        assert "answer_key" not in json.dumps(exported, default=str)
        counts = await handler.erase(db, ectx)
        assert counts["attempts"] == 1
        assert await handler.verify(db, ectx) == []
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(AssessmentInvitation, uuid.UUID(inv["id"]))
        assert row is not None and row.candidate_id is None and row.score == Decimal("100.00") and row.status == "reviewed"
        assert row.skill_scores is None and row.section_scores is None
        assert (await db.execute(select(Attempt).where(Attempt.invitation_id == row.id))).first() is None
    assert (await c.get(f"{INV}/{inv['id']}")).status_code == 404
    assert (await c.get(f"{API}/public/links/{token}/assessment")).status_code in (404, 410)


async def test_candidate_portal_isolation(app: FastAPI, dorg: DOrg) -> None:
    a, _ = await simple_assessment(dorg, ("single",))
    cand_a = await make_candidate(dorg, "Ann Alpha")
    cand_b = await make_candidate(dorg, "Ben Beta")
    inv_a = await invite_ok(dorg, a["id"], cand_a)
    inv_b = await invite_ok(dorg, a["id"], cand_b)
    portal_a = await sign_in_candidate(app, cand_a, dorg.tenant_id)
    try:
        mine = await portal_a.get(f"{API}/portal/assessments")
        assert mine.status_code == 200 and [x["invitation_id"] for x in mine.json()] == [inv_a["id"]]
        assert (await portal_a.get(f"{API}/portal/assessments/{inv_b['id']}")).status_code == 404
        assert (await portal_a.get(f"{API}/portal/assessments/{inv_b['id']}/attempt")).status_code == 404
        pre = await portal_a.get(f"{API}/portal/assessments/{inv_a['id']}")
        assert pre.status_code == 200
        st = await portal_a.post(
            f"{API}/portal/assessments/{inv_a['id']}/start", json={"notice_id": pre.json()["notice"]["notice_id"], "agreed": True}, headers={"X-CSRF-Token": "csrf-token"}
        )
        assert st.status_code == 200, st.text
        assert st.json()["session_id"]
        consent = await sql(dorg, "SELECT source FROM consents WHERE candidate_id = :c AND purpose = 'assessment'", c=cand_a)
        assert consent == [("candidate_portal",)]
    finally:
        await portal_a.aclose()
