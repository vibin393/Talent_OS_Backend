"""Stories 3.3 / 3.4 with a fake AI client: prefill, post-validation, decisions, diff/apply, skips."""

from typing import Any

import anthropic
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.ai import AIAction
from app.models.candidates import TenantSkill
from app.services import ai
from tests.epic3 import CANDIDATES, FakeAI, Org3, create_candidate, h, resume_pdf, upload

DRAFTS = f"{CANDIDATES}/resume-drafts"


async def _draft(org: Org3) -> dict[str, Any]:
    resp = await upload(org.admin.client, DRAFTS, resume_pdf(), "cv.pdf")
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_parse_ok_populates_prefill(org3: Org3, fake_ai: FakeAI) -> None:
    draft = await _draft(org3)
    assert draft["parse_status"] == "parsed"
    pre = draft["prefill"]
    assert pre["full_name"] == "Priya Raman"
    assert [e["email"] for e in pre["emails"]] == ["priya.raman@example.com"]
    assert [p["phone"] for p in pre["phones"]] == ["+919876543210"]
    assert pre["links"] == [{"url": "linkedin.com/in/priya-raman", "kind": "linkedin"}]
    assert pre["location"]["city"] == "Chennai"
    assert pre["current_title"] == "Data Engineer"
    assert pre["employment"][0]["start_date"] == "2022-01-01"
    assert "invalid_email_dropped" in draft["warnings"]
    assert "invalid_phone_dropped" in draft["warnings"]
    resolved = {s["name"] for s in pre["skills"] if s.get("ref")}
    assert resolved == {"Azure Data Factory", "PySpark"}
    assert [u["name"] for u in draft["unresolved_skills"]] == ["FooLangXYZ"]
    assert {"full_name", "emails", "skills", "employment"} <= set(draft["ai_fields"])
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(TenantSkill))).first() is None  # parsing never creates pending skills
        (action,) = (await db.execute(select(AIAction))).scalars()
    assert action.id == draft["resume"]["ai_action_id"] or str(action.id) == draft["resume"]["ai_action_id"]
    assert action.model == get_settings().ai_model
    assert action.prompt_version == "resume_parse.v1"
    assert (action.input_tokens, action.output_tokens) == (120, 80)
    assert action.latency_ms is not None
    assert action.input_ref == f"resume:{draft['resume']['id']}"
    assert "projects" in (action.output or {})


async def _create_from(org: Org3, draft: dict[str, Any], **changes: Any) -> dict[str, Any]:
    body = {k: v for k, v in draft["prefill"].items() if v not in (None, [])}
    body.update(changes)
    body["resume_id"] = draft["resume"]["id"]
    resp = await org.admin.client.post(CANDIDATES, json=body, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _decision(org: Org3, candidate_id: str) -> str | None:
    actions = (await org.admin.client.get(f"{CANDIDATES}/{candidate_id}/ai-actions")).json()
    assert len(actions) == 1
    return actions[0]["decision"]  # type: ignore[no-any-return]


async def test_decision_accepted_when_untouched(org3: Org3, fake_ai: FakeAI) -> None:
    draft = await _draft(org3)
    cand = await _create_from(org3, draft)
    assert await _decision(org3, cand["id"]) == "accepted"
    assert {s["skill"]["name"] for s in cand["skills"]} == {"Azure Data Factory", "PySpark", "FooLangXYZ"}
    foolang = next(s for s in cand["skills"] if s["skill"]["name"] == "FooLangXYZ")
    assert foolang["skill"]["status"] == "pending"  # saved unresolved skills become pending
    assert cand["resumes"][0]["version"] == 1


async def test_decision_edited_with_one_change(org3: Org3, fake_ai: FakeAI) -> None:
    draft = await _draft(org3)
    cand = await _create_from(org3, draft, current_title="Senior Data Engineer")
    assert await _decision(org3, cand["id"]) == "edited"
    actions = (await org3.admin.client.get(f"{CANDIDATES}/{cand['id']}/ai-actions")).json()
    assert actions[0]["prompt_version"] == "resume_parse.v1"
    assert actions[0]["decided_by"] == str(org3.admin.user_id)


async def test_decision_rejected_when_nothing_used(org3: Org3, fake_ai: FakeAI) -> None:
    draft = await _draft(org3)
    resp = await org3.admin.client.post(
        CANDIDATES,
        json={"full_name": "Someone Else", "resume_id": draft["resume"]["id"]},
        headers=h(org3.admin),
    )
    assert resp.status_code == 201
    assert await _decision(org3, resp.json()["id"]) == "rejected"


async def test_feature_disabled_consent_and_provider_errors(org3: Org3, fake_ai: FakeAI) -> None:
    await org3.admin.client.put(
        "/api/v1/ai/settings", json={"features": {"resume_parsing": False}}, headers=h(org3.admin)
    )
    assert (await _draft(org3))["parse_status"] == "skipped_disabled"
    assert fake_ai.calls == []
    await org3.admin.client.put(
        "/api/v1/ai/settings", json={"features": {"resume_parsing": True}}, headers=h(org3.admin)
    )

    async def deny(*_: Any) -> ai.ConsentDecision:
        return ai.ConsentDecision(False, "consent_required")

    ai.set_consent_checker(deny)
    assert (await _draft(org3))["parse_status"] == "skipped_consent"
    assert fake_ai.calls == []
    ai.set_consent_checker(None)

    for error, code in (
        (RuntimeError("down"), "provider_error"),
        (anthropic.APITimeoutError(request=None), "timeout"),  # type: ignore[arg-type]
    ):
        fake_ai.error = error
        draft = await _draft(org3)
        assert draft["parse_status"] == "unavailable"
        async with tenant_session(org3.tenant_id) as db:
            row = await db.get(AIAction, draft["resume"]["ai_action_id"])
        assert row is not None
        assert row.error_code == code
    fake_ai.error = None
    fake_ai.payload = {"full_name": "X", "unexpected": True}
    draft = await _draft(org3)
    assert draft["parse_status"] == "unavailable"


async def test_draft_reports_possible_duplicates(org3: Org3, fake_ai: FakeAI) -> None:
    existing = await create_candidate(
        org3.admin, full_name="P. Raman", emails=[{"email": "priya.raman@example.com"}]
    )
    draft = await _draft(org3)
    assert [d["id"] for d in draft["possible_duplicates"]] == [existing["id"]]


async def test_existing_candidate_diff_and_apply_selected(org3: Org3, fake_ai: FakeAI) -> None:
    cand = await create_candidate(
        org3.admin,
        full_name="Priya Raman",
        current_title="QA Engineer",
        current_employer="Infosys",
        emails=[{"email": "priya.raman@example.com"}],
    )
    up = await upload(org3.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf(), "new.pdf")
    assert up.status_code == 201, up.text
    body = up.json()
    fields = {(d["field"], d.get("index")) for d in body["diff"]}
    assert ("current_title", None) in fields
    assert ("current_employer", None) not in fields  # unchanged
    assert ("emails", None) not in fields  # already present
    assert ("phones", None) in fields
    assert ("employment", 0) in fields
    title = next(d for d in body["diff"] if d["field"] == "current_title")
    assert (title["current"], title["parsed"]) == ("QA Engineer", "Data Engineer")

    applied = await org3.admin.client.post(
        f"{CANDIDATES}/{cand['id']}/resumes/{body['resume']['id']}/apply",
        json={"fields": ["current_title"], "skills": ["Azure Data Factory"], "employment_idx": [1]},
        headers=h(org3.admin),
    )
    assert applied.status_code == 200, applied.text
    out = applied.json()
    assert out["decision"] == "edited"
    after = out["candidate"]
    assert after["current_title"] == "Data Engineer"
    assert after["phones"] == []  # not selected
    assert [s["skill"]["name"] for s in after["skills"]] == ["Azure Data Factory"]
    assert [e["employer"] for e in after["employment"]] == ["TCS"]
    bad = await org3.admin.client.post(
        f"{CANDIDATES}/{cand['id']}/resumes/{body['resume']['id']}/apply",
        json={"fields": ["password"]},
        headers=h(org3.admin),
    )
    assert bad.status_code == 422


async def test_apply_everything_is_accepted(org3: Org3, fake_ai: FakeAI) -> None:
    cand = await create_candidate(org3.admin, full_name="Priya Raman")
    body = (
        await upload(org3.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf(), "cv.pdf")
    ).json()
    diff = body["diff"]
    payload: dict[str, list[Any]] = {"fields": [], "emails": [], "phones": [], "links": [], "skills": []}
    idx: dict[str, list[int]] = {"employment": [], "education": [], "certifications": []}
    for d in diff:
        if d["index"] is not None:
            idx[d["field"]].append(d["index"])
        elif d["field"] in payload:
            payload[d["field"]].append(d["parsed"])
        else:
            payload["fields"].append(d["field"])
    resp = await org3.admin.client.post(
        f"{CANDIDATES}/{cand['id']}/resumes/{body['resume']['id']}/apply",
        json={
            **payload,
            "employment_idx": idx["employment"],
            "education_idx": idx["education"],
            "certification_idx": idx["certifications"],
        },
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "accepted"
    assert resp.json()["candidate"]["location"]["city"] == "Chennai"
