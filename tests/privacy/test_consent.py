"""Story 12.1 (FR-67): AI-processing basis, gateway consent checker, "AI consent pending" flag, consent
requests via secure links, withdrawal purge, versioned notices. Matrix rows 1-4, 35."""

import re
import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core import audit
from app.core.database import tenant_session
from app.core.pagination import PageParams
from app.core.settings import get_settings
from app.models.ai import AIAction
from app.models.privacy import Consent
from app.services import privacy
from app.services.privacy import registry
from tests.epic12 import (
    CANDIDATES,
    PRIVACY,
    FakeAI,
    Org3,
    candidate_row,
    create_candidate,
    h,
    member,
    resume_pdf,
    sent,
    set_basis,
    upload,
)

LINK_RE = re.compile(re.escape(get_settings().frontend_url.rstrip("/")) + r"/l/([A-Za-z0-9_-]+)")


async def _grant(org: Org3, candidate_id: str, purpose: str = "ai_processing") -> None:
    from app.core.context import RequestContext

    async with tenant_session(org.tenant_id) as db:
        await privacy.grant_consent(
            RequestContext(tenant_id=org.tenant_id), db, uuid.UUID(candidate_id), purpose, source="recruiter_attestation"
        )


async def _last_action(org: Org3) -> AIAction:
    async with tenant_session(org.tenant_id) as db:
        return (await db.execute(select(AIAction).order_by(AIAction.created_at.desc()).limit(1))).scalar_one()


async def test_consent_required_without_consent_skips_parsing(org3: Org3, fake_ai: FakeAI) -> None:
    """Matrix 1."""
    cand = await create_candidate(org3.admin, emails=[{"email": "ravi@example.com"}])
    assert cand["ai_consent_state"] == "pending"
    resp = await upload(org3.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf())
    assert resp.status_code == 201, resp.text
    assert resp.json()["resume"]["parse_status"] == "skipped_consent"
    action = await _last_action(org3)
    assert (action.status, action.error_code, action.consent_basis) == ("skipped", "consent_missing", "denied")
    assert fake_ai.calls == []
    assert (await candidate_row(org3.tenant_id, cand["id"])).ai_consent_state == "pending"
    # Facet + flag on the list.
    listed = await org3.admin.client.get(CANDIDATES, params={"ai_consent_state": "pending"})
    assert cand["id"] in {c["id"] for c in listed.json()["items"]}


async def test_notice_based_recruiter_sourced_runs_with_notice_basis(org3: Org3, fake_ai: FakeAI) -> None:
    """Matrix 2."""
    await set_basis(org3.tenant_id, "notice_based")
    cand = await create_candidate(org3.admin)
    assert cand["ai_consent_state"] == "not_required"
    resp = await upload(org3.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf())
    assert resp.json()["resume"]["parse_status"] == "parsed", resp.text
    assert (await _last_action(org3)).consent_basis == "notice"
    assert len(fake_ai.calls) == 1


async def test_granted_consent_allows_and_withdrawal_denies_and_purges(org3: Org3, fake_ai: FakeAI) -> None:
    """Matrix 3: the next AI call is denied at once; derived data is purged (task + sweep)."""
    purged: list[uuid.UUID] = []
    derived: set[uuid.UUID] = set()

    async def purge(db: Any, candidate_id: uuid.UUID) -> dict[str, int]:
        purged.append(candidate_id)
        derived.discard(candidate_id)
        return {"rows": 1}

    async def find(db: Any, ids: Any) -> set[uuid.UUID]:
        return {i for i in ids if i in derived}

    registry.register_ai_derived_purger("test.embeddings", purge, find)
    try:
        cand = await create_candidate(org3.admin)
        cid = uuid.UUID(cand["id"])
        await _grant(org3, cand["id"])
        assert (await candidate_row(org3.tenant_id, cid)).ai_consent_state == "granted"
        ok = await upload(org3.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf())
        assert ok.json()["resume"]["parse_status"] == "parsed"
        assert (await _last_action(org3)).consent_basis == "consent"

        consents = (await org3.admin.client.get(f"/api/v1/candidates/{cid}/consents")).json()
        (granted,) = [c for c in consents if c["purpose"] == "ai_processing"]
        derived.add(cid)
        resp = await org3.admin.client.post(f"/api/v1/consents/{granted['id']}/withdraw", headers=h(org3.admin))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "withdrawn"
        assert purged == [cid]  # ConsentWithdrawn -> purge_ai_derived (eager)
        assert (await candidate_row(org3.tenant_id, cid)).ai_consent_state == "withdrawn"

        denied = await upload(org3.admin.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf())
        assert denied.json()["resume"]["parse_status"] == "skipped_consent"
        assert (await _last_action(org3)).consent_basis == "denied"

        # Withdrawn always denies, even under notice_based.
        await set_basis(org3.tenant_id, "notice_based")
        async with tenant_session(org3.tenant_id) as db:
            decision = await privacy.ai_consent_checker(None, db, "resume_parsing", ("candidate", cid))  # type: ignore[arg-type]
        assert (decision.allowed, decision.basis) == (False, "denied")

        # The 15-minute sweep re-purges candidates that still have derived rows.
        derived.add(cid)
        from app.tasks.privacy import consent_withdrawal_sweep

        consent_withdrawal_sweep.delay(str(org3.tenant_id))
        assert purged == [cid, cid]
    finally:
        registry._purgers.pop("test.embeddings", None)


async def test_subjectless_calls_are_not_applicable(org3: Org3) -> None:
    async with tenant_session(org3.tenant_id) as db:
        decision = await privacy.ai_consent_checker(None, db, "jd_generation", None)  # type: ignore[arg-type]
    assert (decision.allowed, decision.basis) == (True, "not_applicable")


async def test_consent_requests_sent_skipped_and_granted_via_link(app: FastAPI, org3: Org3) -> None:
    """Matrix 4: three candidates, one without an email -> 2 sent, 1 skipped_no_email; the link grants."""
    a = await create_candidate(org3.admin, full_name="Anu A", emails=[{"email": "anu@example.com"}])
    b = await create_candidate(org3.admin, full_name="Bala B", emails=[{"email": "bala@example.com"}])
    c = await create_candidate(org3.admin, full_name="Chitra C")
    body = {"candidate_ids": [a["id"], b["id"], c["id"]], "purpose": "ai_processing"}
    resp = await org3.admin.client.post(f"{PRIVACY}/consent-requests", json=body, headers=h(org3.admin))
    assert resp.status_code == 200, resp.text
    statuses = {i["candidate_id"]: i["status"] for i in resp.json()["items"]}
    assert statuses == {a["id"]: "sent", b["id"]: "sent", c["id"]: "skipped_no_email"}
    assert resp.json()["counts"] == {"sent": 2, "skipped_no_email": 1}
    again = await org3.admin.client.post(f"{PRIVACY}/consent-requests", json=body, headers=h(org3.admin))
    assert {i["status"] for i in again.json()["items"]} == {"skipped_recent", "skipped_no_email"}

    (mail,) = sent("consent_request", to="anu@example.com")
    token = LINK_RE.search(mail.text).group(1)  # type: ignore[union-attr]
    assert "anu" not in token.lower()
    public = await org3.admin.client.get(f"/api/v1/public/links/{token}")
    assert public.status_code == 200, public.text
    assert public.headers["Cache-Control"] == "no-store"
    assert public.json()["notice"]["purpose"] == "ai_processing"
    origin = {"Origin": get_settings().frontend_url.rstrip("/")}
    answer = await org3.admin.client.post(
        f"/api/v1/public/links/{token}/consent", json={"granted": True}, headers=origin
    )
    assert answer.json() == {"status": "granted"}
    assert (await candidate_row(org3.tenant_id, a["id"])).ai_consent_state == "granted"
    reused = await org3.admin.client.post(
        f"/api/v1/public/links/{token}/consent", json={"granted": True}, headers=origin
    )
    assert (reused.status_code, reused.json()["error"]["code"]) == (410, "link_used")
    already = await org3.admin.client.post(
        f"{PRIVACY}/consent-requests", json={"candidate_ids": [a["id"]]}, headers=h(org3.admin)
    )
    assert already.json()["items"][0]["status"] == "skipped_already_granted"
    async with tenant_session(org3.tenant_id) as db:
        row = (await db.execute(select(Consent).where(Consent.candidate_id == uuid.UUID(a["id"])))).scalar_one()
    assert (row.source, row.notice_id is not None) == ("candidate_link", True)


async def test_basis_endpoint_recomputes_flags_and_is_audited(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    resp = await org3.admin.client.get(f"{PRIVACY}/ai-basis")
    assert resp.json()["basis"] == "consent_required"
    assert resp.json()["pending_count"] >= 1
    put = await org3.admin.client.put(f"{PRIVACY}/ai-basis", json={"basis": "notice_based"}, headers=h(org3.admin))
    assert put.status_code == 200, put.text
    assert (await candidate_row(org3.tenant_id, cand["id"])).ai_consent_state == "not_required"
    async with tenant_session(org3.tenant_id) as db:
        page = await audit.search(db, audit.AuditFilters(action="privacy.ai_basis_changed"), PageParams())
    assert page.total == 1


async def test_notice_versions_one_active(org3: Org3) -> None:
    listed = (await org3.admin.client.get(f"{PRIVACY}/consent-notices", params={"purpose": "ai_processing"})).json()
    assert [(n["version"], n["active"]) for n in listed] == [(1, True)]
    created = await org3.admin.client.post(
        f"{PRIVACY}/consent-notices",
        json={"purpose": "ai_processing", "title": "AI processing v2", "body_md": "Updated text"},
        headers=h(org3.admin),
    )
    assert created.status_code == 201, created.text
    assert (created.json()["version"], created.json()["active"]) == (2, False)
    act = await org3.admin.client.post(f"{PRIVACY}/consent-notices/{created.json()['id']}/activate", headers=h(org3.admin))
    assert act.json()["active"] is True
    listed = (await org3.admin.client.get(f"{PRIVACY}/consent-notices", params={"purpose": "ai_processing"})).json()
    assert [(n["version"], n["active"]) for n in listed] == [(2, True), (1, False)]
    bad = await org3.admin.client.post(
        f"{PRIVACY}/consent-notices",
        json={"purpose": "ai_processing", "locale": "xx", "title": "t", "body_md": "b"},
        headers=h(org3.admin),
    )
    assert bad.status_code == 422


async def test_permissions_and_cross_tenant(app: FastAPI, org3: Org3, other3: Org3) -> None:
    """Matrix 35 for the consent endpoints."""
    cand = await create_candidate(org3.admin)
    other_cand = await create_candidate(other3.admin)
    assert (await org3.admin.client.get(f"/api/v1/candidates/{other_cand['id']}/consents")).status_code == 404
    resp = await org3.admin.client.post(
        f"{PRIVACY}/consent-requests", json={"candidate_ids": [other_cand["id"]]}, headers=h(org3.admin)
    )
    assert resp.json()["items"][0]["status"] == "not_found"
    notice = (await other3.admin.client.get(f"{PRIVACY}/consent-notices")).json()[0]
    assert (await org3.admin.client.post(f"{PRIVACY}/consent-notices/{notice['id']}/activate", headers=h(org3.admin))).status_code == 404
    assert (await org3.admin.client.post(f"/api/v1/consents/{uuid.uuid4()}/withdraw", headers=h(org3.admin))).status_code == 404
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await recruiter.client.get(f"{PRIVACY}/ai-basis")).status_code == 403
        assert (await recruiter.client.get(f"/api/v1/candidates/{cand['id']}/consents")).status_code == 403
    finally:
        await recruiter.client.aclose()
