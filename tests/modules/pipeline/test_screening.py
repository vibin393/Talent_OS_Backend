"""Recruiter screening (6.2) and AI screening over secure links (6.3, NFR-17)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from app.core import secure_links
from app.core.database import tenant_session
from app.core.mailer import OutboxMailer
from app.core.settings import get_settings
from app.models.pipeline import Screening
from app.services.pipeline import handlers as pipeline_handlers
from tests.epic3 import FakeAI
from tests.factories import deferred_enqueues
from tests.modules.pipeline.helpers import API, LINK_RE, POrg, add_one, create_candidate, h, rows, stage_of

QUESTIONS = {
    "questions": [
        {"key": "pyspark_years", "label": "Years of PySpark?", "type": "number", "required": True},
        {"key": "notice", "label": "Notice period in days?", "type": "number", "required": True},
        {"key": "remote", "label": "Preferred work mode?", "type": "choice", "options": ["remote", "hybrid"]},
        {"key": "project", "label": "Describe a recent data pipeline.", "type": "text", "required": True},
        {"key": "relocate", "label": "Open to Chennai?", "type": "text", "required": False},
    ]
}
SUMMARY = {
    "structured_answers": [{"question_key": "pyspark_years", "answer": "3"}],
    "gaps": [{"question_key": "relocate", "issue": "Not answered"}],
    "summary": "Three years of PySpark; 30 days notice.",
    "suggested_outcome": "proceed",
}
ANSWERS = {"pyspark_years": 3, "notice": 30, "remote": "hybrid", "project": "Built a lakehouse on ADF."}


# ================================================================ recruiter screening
async def test_form_has_default_and_skill_questions(porg: POrg) -> None:
    app_id = await add_one(porg.admin, porg.demand_id, (await create_candidate(porg.admin))["id"])
    form = await porg.admin.client.get(f"{API}/applications/{app_id}/screening-form")
    assert form.status_code == 200, form.text
    keys = [q["key"] for q in form.json()["questions"]]
    assert {"interest", "expected_compensation", "notice_period", "communication"} <= set(keys)
    skill = next(q for q in form.json()["questions"] if q["key"].startswith("skill_"))
    assert skill["label"] == "Years of hands-on experience with PySpark?" and skill["required"] is True


async def test_screening_reject_moves_to_rejected(porg: POrg) -> None:
    app_id = await add_one(porg.admin, porg.demand_id, (await create_candidate(porg.admin))["id"])
    resp = await porg.admin.client.post(
        f"{API}/applications/{app_id}/screenings",
        json={
            "answers": {"interest": False, "skill_pyspark_years": 0},
            "outcome": "reject",
            "reason_code": "not_qualified",
        },
        headers=h(porg.admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "completed" and resp.json()["outcome"] == "reject"
    assert await stage_of(porg.admin, app_id) == "rejected"


async def test_screening_proceed_and_candidate_updates_need_confirmation(porg: POrg) -> None:
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    created = await porg.admin.client.post(
        f"{API}/applications/{app_id}/screenings",
        json={
            "answers": {
                "interest": True,
                "notice_period": 60,
                "expected_compensation": {"amount": "2400000", "currency": "INR"},
                "relocation": "yes",
                "skill_pyspark_years": 3,
            },
            "notes": "Good call",
        },
        headers=h(porg.admin),
    )
    assert created.status_code == 201, created.text
    screening = created.json()
    assert screening["status"] == "draft"
    proposed = {p["field"]: p for p in screening["proposed_candidate_updates"]}
    assert (
        proposed["notice_period_days"]["current"] == 30 and proposed["notice_period_days"]["proposed"] == 60
    )

    outcome = await porg.admin.client.post(
        f"{API}/screenings/{screening['id']}/outcome",
        json={"outcome": "proceed", "move_to_shortlisted": True},
        headers=h(porg.admin),
    )
    assert outcome.status_code == 200, outcome.text
    assert await stage_of(porg.admin, app_id) == "shortlisted"
    detail = (await porg.admin.client.get(f"{API}/applications/{app_id}")).json()
    assert detail["screening_passed"] is True

    # Nothing written to the candidate until the recruiter confirms.
    before = (await porg.admin.client.get(f"{API}/candidates/{cand['id']}")).json()
    assert before["notice_period_days"] == 30
    applied = await porg.admin.client.post(
        f"{API}/screenings/{screening['id']}/apply-candidate-updates",
        json={"fields": ["notice_period_days", "willing_to_relocate"]},
        headers=h(porg.admin),
    )
    assert applied.status_code == 200, applied.text
    after = (await porg.admin.client.get(f"{API}/candidates/{cand['id']}")).json()
    assert after["notice_period_days"] == 60 and after["willing_to_relocate"] is True

    again = await porg.admin.client.post(
        f"{API}/screenings/{screening['id']}/outcome", json={"outcome": "hold"}, headers=h(porg.admin)
    )
    assert again.status_code == 409


async def test_invalid_answers(porg: POrg) -> None:
    app_id = await add_one(porg.admin, porg.demand_id, (await create_candidate(porg.admin))["id"])
    resp = await porg.admin.client.post(
        f"{API}/applications/{app_id}/screenings",
        json={"answers": {"communication": 9}},
        headers=h(porg.admin),
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_answers"
    missing = await porg.admin.client.post(
        f"{API}/applications/{app_id}/screenings",
        json={"answers": {"notice_period": 10}, "outcome": "hold"},
        headers=h(porg.admin),
    )
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "answers_required"


# ================================================================ AI screening
async def _ai_screening(
    porg: POrg, fake_ai: FakeAI, outbox: OutboxMailer
) -> tuple[str, str, str, dict[str, Any]]:
    cand = await create_candidate(porg.admin)
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    fake_ai.payload = QUESTIONS
    gen = await porg.admin.client.post(
        f"{API}/applications/{app_id}/ai-screenings", json={"action": "generate"}, headers=h(porg.admin)
    )
    assert gen.status_code == 201, gen.text
    screening = gen.json()
    assert screening["kind"] == "ai" and screening["status"] == "draft" and len(screening["questions"]) == 5
    prompt = fake_ai.calls[-1]["messages"][0]["content"]
    assert "Priya" not in prompt and "PySpark" in prompt  # demand facts only, no candidate data
    outbox.sent.clear()
    sent = await porg.admin.client.post(f"{API}/screenings/{screening['id']}/send", headers=h(porg.admin))
    assert sent.status_code == 200, sent.text
    mails = [m for m in outbox.sent if m.kind == "ai_screening_invite"]
    assert len(mails) == 1
    token = LINK_RE.search(mails[0].text).group(1)  # type: ignore[union-attr]
    return app_id, screening["id"], token, cand


async def test_ai_screening_end_to_end(
    porg: POrg, fake_ai: FakeAI, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    app_id, sid, token, cand = await _ai_screening(porg, fake_ai, outbox)
    url = f"{API}/public/links/{token}"
    assert cand["full_name"] not in url and "@" not in url

    view = await public.get(url)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["purpose"] == "ai_screening" and body["consent_required"] is True
    assert body["job_title"] == "Senior Data Engineer" and body["client_name"] is None
    assert body["notice"]["purpose"] == "ai_processing"
    assert view.headers["cache-control"] == "no-store" and view.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in view.headers["x-robots-tag"]
    # viewing twice changes nothing
    assert (await public.get(url)).status_code == 200

    early = await public.post(f"{url}/submit", json={"answers": ANSWERS})
    assert early.status_code == 409 and early.json()["error"]["code"] == "consent_required"
    consent = await public.post(f"{url}/consent", json={"granted": True, "notice_id": body["notice"]["id"]})
    assert consent.status_code == 200 and consent.json()["status"] == "granted"
    incomplete = await public.post(f"{url}/submit", json={"answers": {"notice": 30}})
    assert incomplete.status_code == 422

    fake_ai.payload = {**SUMMARY, "suggested_outcome": "reject"}  # the AI can never suggest rejection
    done = await public.post(f"{url}/submit", json={"answers": ANSWERS})
    assert done.status_code == 200, done.text
    twice = await public.post(f"{url}/submit", json={"answers": ANSWERS})
    assert twice.status_code == 410 and twice.json()["error"]["code"] == "link_used"

    s = (await porg.admin.client.get(f"{API}/screenings/{sid}")).json()
    assert s["status"] == "answered" and s["answers"]["pyspark_years"] == 3
    assert s["ai_generated"] is True and s["ai_summary"]["ai_generated"] is True
    assert s["ai_summary"]["suggested_outcome"] is None
    assert s["ai_action_id"]
    assert await stage_of(porg.admin, app_id) == "applied"  # AI never moves anything

    blocked = await porg.admin.client.post(
        f"{API}/screenings/{sid}/outcome", json={"outcome": "proceed"}, headers=h(porg.admin)
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "review_required"
    reviewed = await porg.admin.client.post(f"{API}/screenings/{sid}/review", headers=h(porg.admin))
    assert reviewed.status_code == 200 and reviewed.json()["reviewed_at"]
    ok = await porg.admin.client.post(
        f"{API}/screenings/{sid}/outcome",
        json={"outcome": "proceed", "move_to_shortlisted": True},
        headers=h(porg.admin),
    )
    assert ok.status_code == 200, ok.text
    assert await stage_of(porg.admin, app_id) == "shortlisted"

    consents = (await porg.admin.client.get(f"{API}/candidates/{cand['id']}/consents")).json()
    assert any(c["purpose"] == "ai_processing" and c["source"] == "candidate_link" for c in consents)


async def test_summary_failure_and_retry(
    porg: POrg, fake_ai: FakeAI, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    _, sid, token, _ = await _ai_screening(porg, fake_ai, outbox)
    url = f"{API}/public/links/{token}"
    await public.post(f"{url}/consent", json={"granted": True})
    fake_ai.payload = {"not": "valid"}
    assert (await public.post(f"{url}/submit", json={"answers": ANSWERS})).status_code == 200
    s = (await porg.admin.client.get(f"{API}/screenings/{sid}")).json()
    assert s["ai_summary"] is None and s["summary_status"] == "failed" and s["answers"]
    fake_ai.payload = SUMMARY
    retry = await porg.admin.client.post(f"{API}/screenings/{sid}/summarize", headers=h(porg.admin))
    assert retry.status_code == 200
    s = (await porg.admin.client.get(f"{API}/screenings/{sid}")).json()
    assert s["ai_summary"]["suggested_outcome"] == "proceed" and s["summary_status"] == "ready"


async def test_ai_unavailable_is_503(porg: POrg) -> None:
    app_id = await add_one(porg.admin, porg.demand_id, (await create_candidate(porg.admin))["id"])
    resp = await porg.admin.client.post(
        f"{API}/applications/{app_id}/ai-screenings", json={"action": "generate"}, headers=h(porg.admin)
    )
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "ai_unavailable"


async def test_send_requires_candidate_email(porg: POrg, fake_ai: FakeAI) -> None:
    cand = await create_candidate(porg.admin, emails=[])
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    fake_ai.payload = QUESTIONS
    gen = await porg.admin.client.post(
        f"{API}/applications/{app_id}/ai-screenings", json={"action": "generate"}, headers=h(porg.admin)
    )
    sent = await porg.admin.client.post(f"{API}/screenings/{gen.json()['id']}/send", headers=h(porg.admin))
    assert sent.status_code == 409 and sent.json()["error"]["code"] == "candidate_email_missing"


async def test_decline_consumes_link(
    porg: POrg, fake_ai: FakeAI, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    _, sid, token, _ = await _ai_screening(porg, fake_ai, outbox)
    url = f"{API}/public/links/{token}"
    declined = await public.post(f"{url}/consent", json={"granted": False})
    assert declined.status_code == 200 and declined.json()["status"] == "declined"
    assert (await public.get(url)).status_code == 410
    s = (await porg.admin.client.get(f"{API}/screenings/{sid}")).json()
    assert s["status"] == "consent_declined"


async def test_link_errors(
    porg: POrg, fake_ai: FakeAI, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    _, sid, token, _ = await _ai_screening(porg, fake_ai, outbox)
    url = f"{API}/public/links/{token}"
    unknown = await public.get(f"{API}/public/links/{'x' * 43}")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "link_invalid"
    wrong_purpose = await public.post(f"{url}/feedback", json={"decision": "interested"})
    assert wrong_purpose.status_code == 404

    # Only the SHA-256 of the token is stored.
    links = await rows(
        porg.tenant_id,
        select(secure_links.SecureLink).where(secure_links.SecureLink.subject_id == uuid.UUID(sid)),
    )
    assert links and all(
        link.token_hash == secure_links.hash_link_token(token) or link.revoked_at for link in links
    )
    assert all(token.encode() not in link.token_hash for link in links)

    async with tenant_session(porg.tenant_id) as db:
        await db.execute(
            update(secure_links.SecureLink)
            .where(secure_links.SecureLink.token_hash == secure_links.hash_link_token(token))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    expired = await public.get(url)
    assert expired.status_code == 410 and expired.json()["error"]["code"] == "link_expired"

    # Resending revokes the previous link.
    outbox.sent.clear()
    async with tenant_session(porg.tenant_id) as db:
        await db.execute(update(Screening).where(Screening.id == uuid.UUID(sid)).values(status="expired"))
    resend = await porg.admin.client.post(f"{API}/screenings/{sid}/send", headers=h(porg.admin))
    assert resend.status_code == 200
    new_token = LINK_RE.search(next(m for m in outbox.sent if m.kind == "ai_screening_invite").text).group(1)  # type: ignore[union-attr]
    link_id = (
        resend.json()["secure_link_id"]
        or (await porg.admin.client.get(f"{API}/screenings/{sid}")).json()["secure_link_id"]
    )
    revoked = await porg.admin.client.post(f"{API}/secure-links/{link_id}/revoke", headers=h(porg.admin))
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    gone = await public.get(f"{API}/public/links/{new_token}")
    assert gone.status_code == 410 and gone.json()["error"]["code"] == "link_revoked"


async def test_origin_required_and_rate_limits(
    porg: POrg,
    fake_ai: FakeAI,
    outbox: OutboxMailer,
    public: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    _, _, token, _ = await _ai_screening(porg, fake_ai, outbox)
    url = f"{API}/public/links/{token}"
    no_origin = await public.post(
        f"{url}/consent", json={"granted": True}, headers={"Origin": "https://evil.example"}
    )
    assert no_origin.status_code == 403
    codes = [(await public.get(url)).status_code for _ in range(21)]
    assert codes[0] == 200 and codes[-1] == 429  # 20 per minute per token
    assert (await public.get(url)).json()["error"]["code"] == "rate_limited"


async def test_parallel_submit_race(
    porg: POrg, fake_ai: FakeAI, outbox: OutboxMailer, public: httpx.AsyncClient
) -> None:
    _, sid, token, _ = await _ai_screening(porg, fake_ai, outbox)
    url = f"{API}/public/links/{token}"
    await public.post(f"{url}/consent", json={"granted": True})
    fake_ai.payload = SUMMARY
    with deferred_enqueues(pipeline_handlers):
        first, second = await asyncio.gather(
            public.post(f"{url}/submit", json={"answers": ANSWERS}),
            public.post(f"{url}/submit", json={"answers": ANSWERS}),
        )
    assert sorted([first.status_code, second.status_code]) in ([200, 409], [200, 410])
    s = await rows(porg.tenant_id, select(Screening).where(Screening.id == uuid.UUID(sid)))
    assert s[0].status in ("answered", "reviewed")
