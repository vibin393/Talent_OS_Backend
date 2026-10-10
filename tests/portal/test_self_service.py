"""Story 9.4 (FR-50): profile, resumes, applications with stage labels + timeline, withdraw, interviews,
AI screenings; every endpoint scoped to the signed-in candidate (others -> 404)."""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.candidates import Candidate
from app.models.pipeline import Application, Screening
from app.services import pipeline_service
from tests.epic3 import FakeAI, create_candidate, resume_pdf
from tests.modules.demands.helpers import admin_ctx, h
from tests.portal.conftest import ADMIN_SITE, PORTAL, Site, apply, ch, publish, sign_in

EMAIL = "priya.raman@example.com"


async def _applied(
    app: FastAPI, site: Site, public: httpx.AsyncClient, **kw: object
) -> tuple[httpx.AsyncClient, dict]:  # type: ignore[type-arg]
    p = await publish(site, **kw)  # type: ignore[arg-type]
    assert (await apply(public, site.slug, p["posting_id"])).status_code == 202
    return await sign_in(app, site.slug, EMAIL), p


async def _move(site: Site, application_id: str, *keys: str) -> None:
    ctx = admin_ctx(site.org)
    async with tenant_session(site.tenant_id) as db:
        for key in keys:
            actor = "system" if key in ("submitted", "offered", "hired") else "user"
            await pipeline_service.transition_application(ctx, db, uuid.UUID(application_id), key, actor)  # type: ignore[arg-type]


async def test_applications_labels_timeline_and_withdraw(
    app: FastAPI, site: Site, public: httpx.AsyncClient
) -> None:
    client, _ = await _applied(app, site, public)
    try:
        apps = (await client.get(f"{PORTAL}/applications")).json()
        assert len(apps) == 1
        a = apps[0]
        assert (
            a["stage_label"] == "Application received"
            and a["status"] == "active"
            and a["can_withdraw"] is True
        )
        assert a["title"] == "Senior Data Engineer" and a["client_name"] is None
        assert "stage_key" not in a
        await _move(site, a["id"], "shortlisted", "submitted")
        detail = (await client.get(f"{PORTAL}/applications/{a['id']}")).json()
        assert detail["stage_label"] == "Under review"
        assert [t["label"] for t in detail["timeline"]] == ["Application received", "Under review"]

        await site.admin.client.patch(
            ADMIN_SITE,
            json={"candidate_stage_labels": {"submitted": "With the hiring team"}},
            headers=h(site.admin),
        )
        assert (await client.get(f"{PORTAL}/applications/{a['id']}")).json()[
            "stage_label"
        ] == "With the hiring team"

        reasons = (await client.get(f"{PORTAL}/withdraw-reasons")).json()
        assert {"code": "personal", "label": "Personal reasons"} in reasons
        bad = await client.post(
            f"{PORTAL}/applications/{a['id']}/withdraw", json={"reason_code": "nope"}, headers=ch(client)
        )
        assert bad.status_code == 422
        ok = await client.post(
            f"{PORTAL}/applications/{a['id']}/withdraw", json={"reason_code": "personal"}, headers=ch(client)
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["stage_label"] == "Withdrawn" and ok.json()["status"] == "closed"
        again = await client.post(
            f"{PORTAL}/applications/{a['id']}/withdraw", json={"reason_code": "personal"}, headers=ch(client)
        )
        assert (again.status_code, again.json()["error"]["code"]) == (409, "application_closed")
    finally:
        await client.aclose()


async def test_withdraw_hired_is_409(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    client, _ = await _applied(app, site, public)
    try:
        a = (await client.get(f"{PORTAL}/applications")).json()[0]
        await _move(site, a["id"], "shortlisted", "submitted", "interviewing", "selected", "offered", "hired")
        r = await client.post(
            f"{PORTAL}/applications/{a['id']}/withdraw", json={"reason_code": "personal"}, headers=ch(client)
        )
        assert (r.status_code, r.json()["error"]["code"]) == (409, "application_closed")
        assert (await client.get(f"{PORTAL}/applications")).json()[0]["stage_label"] == "Hired"
    finally:
        await client.aclose()


async def test_profile_view_and_edit(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    client, _ = await _applied(app, site, public)
    try:
        prof = (await client.get(f"{PORTAL}/profile")).json()
        assert prof["full_name"] == "Priya Raman" and prof["emails"] == [EMAIL]
        upd = await client.patch(
            f"{PORTAL}/profile",
            json={
                "headline": "Lead Data Engineer",
                "phones": ["+91 98765 43210"],
                "links": ["https://github.com/priya"],
                "notice_period_days": 30,
                "expected_compensation": {"amount": "2500000", "currency": "INR", "unit": "annual"},
                "location": {"city": "Chennai"},
            },
            headers=ch(client),
        )
        assert upd.status_code == 200, upd.text
        body = upd.json()
        assert body["headline"] == "Lead Data Engineer" and body["phones"] == ["+919876543210"]
        assert body["expected_compensation"] == {"amount": "2500000.00", "currency": "INR", "unit": "annual"}
        email = await client.patch(
            f"{PORTAL}/profile", json={"emails": ["new@example.com"]}, headers=ch(client)
        )
        assert (email.status_code, email.json()["error"]["code"]) == (422, "email_change_via_request")
        timeline = (await site.admin.client.get(f"/api/v1/candidates/{await _cid(site)}/timeline")).json()
        assert any("Updated by candidate" in (i.get("body") or "") for i in timeline["items"])
    finally:
        await client.aclose()


async def _cid(site: Site) -> str:
    async with tenant_session(site.tenant_id) as db:
        return str((await db.execute(select(Candidate.id))).scalars().first())


async def test_resumes_upload_list_download_no_ai_without_consent(
    app: FastAPI, site: Site, public: httpx.AsyncClient, fake_ai: FakeAI
) -> None:
    client, _ = await _applied(app, site, public)
    try:
        listed = (await client.get(f"{PORTAL}/resumes")).json()
        assert len(listed) == 1
        up = await client.post(
            f"{PORTAL}/resumes",
            files={"file": ("new.pdf", resume_pdf(), "application/pdf")},
            headers=ch(client),
        )
        assert up.status_code == 201, up.text
        assert up.json()["version"] == 2
        assert fake_ai.calls == []  # no ai_processing consent
        dl = await client.get(up.json()["download_url"])
        assert dl.status_code == 200 and dl.content.startswith(b"%PDF")
        other = await client.get(f"{PORTAL}/resumes/{uuid.uuid4()}/download")
        assert other.status_code == 404
    finally:
        await client.aclose()


async def test_interviews_empty_or_upcoming(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    client, _ = await _applied(app, site, public)
    try:
        r = await client.get(f"{PORTAL}/interviews", params={"upcoming": "true"})
        assert r.status_code == 200 and isinstance(r.json(), list)
        for i in r.json():
            assert set(i) <= {
                "id",
                "title",
                "round_name",
                "starts_at",
                "ends_at",
                "timezone",
                "mode",
                "location",
                "join_url",
                "status",
            }
    finally:
        await client.aclose()


async def test_ai_screening_consent_and_submit(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    from app.core import secure_links

    client, _ = await _applied(app, site, public)
    try:
        async with tenant_session(site.tenant_id) as db:
            appl = (await db.execute(select(Application))).scalar_one()
            screening = Screening(
                id=uuid.uuid4(), tenant_id=site.tenant_id, application_id=appl.id, candidate_id=appl.candidate_id,
                kind="ai", status="sent", questions=[{"key": "why", "label": "Why this role?", "type": "text", "required": True}],
                ai_summary={"summary": "SENTINEL_AI_SUMMARY"},
            )  # fmt: skip
            db.add(screening)
            await db.flush()
            link, _ = await secure_links.issue(
                db, tenant_id=site.tenant_id, purpose="ai_screening", subject_type="screening",
                subject_id=screening.id, ttl=__import__("datetime").timedelta(days=3),
            )  # fmt: skip
            screening.secure_link_id = link.id
            sid = screening.id
        listed = (await client.get(f"{PORTAL}/ai-screenings")).text
        assert str(sid) in listed and "SENTINEL_AI_SUMMARY" not in listed
        detail = (await client.get(f"{PORTAL}/ai-screenings/{sid}")).json()
        assert detail["consent_required"] is True and detail["questions"][0]["key"] == "why"
        assert "ai_summary" not in detail and "outcome" not in detail
        early = await client.post(
            f"{PORTAL}/ai-screenings/{sid}/submit", json={"answers": {"why": "x"}}, headers=ch(client)
        )
        assert early.status_code == 409
        consent = await client.post(
            f"{PORTAL}/ai-screenings/{sid}/consent",
            json={"granted": True, "notice_id": detail["notice"]["id"]},
            headers=ch(client),
        )
        assert consent.json() == {"status": "granted"}
        done = await client.post(
            f"{PORTAL}/ai-screenings/{sid}/submit",
            json={"answers": {"why": "I love data"}},
            headers=ch(client),
        )
        assert done.json() == {"status": "answered"}
        async with tenant_session(site.tenant_id) as db:
            used = await db.get(secure_links.SecureLink, link.id)
            assert used is not None and used.use_count == 1
    finally:
        await client.aclose()


async def test_cross_candidate_and_cross_tenant_404(
    app: FastAPI, site: Site, other_site: Site, public: httpx.AsyncClient
) -> None:
    client, p = await _applied(app, site, public)
    # A second candidate in the same tenant.
    other_resp = await apply(
        public, site.slug, p["posting_id"], email="someone.else@example.com", full_name="Someone Else"
    )
    assert other_resp.status_code == 202
    other = await sign_in(app, site.slug, "someone.else@example.com")
    # And a candidate in another tenant with the same email.
    p2 = await publish(other_site)
    assert (await apply(public, other_site.slug, p2["posting_id"])).status_code == 202
    foreign = await sign_in(app, other_site.slug, EMAIL)
    try:
        mine = (await client.get(f"{PORTAL}/applications")).json()[0]["id"]
        for c in (other, foreign):
            assert (await c.get(f"{PORTAL}/applications/{mine}")).status_code == 404
            w = await c.post(
                f"{PORTAL}/applications/{mine}/withdraw", json={"reason_code": "personal"}, headers=ch(c)
            )
            assert w.status_code == 404
            assert (await c.get(f"{PORTAL}/ai-screenings/{uuid.uuid4()}")).status_code == 404
            assert (await c.get(f"{PORTAL}/data-exports/{uuid.uuid4()}/download")).status_code == 404
            assert (
                await c.post(f"{PORTAL}/consents/{uuid.uuid4()}/withdraw", headers=ch(c))
            ).status_code == 404
        resumes = (await client.get(f"{PORTAL}/resumes")).json()
        assert (await other.get(resumes[0]["download_url"])).status_code == 404
        assert (await foreign.get(resumes[0]["download_url"])).status_code == 404
        assert len((await foreign.get(f"{PORTAL}/applications")).json()) == 1
    finally:
        for c in (client, other, foreign):
            await c.aclose()


@pytest.mark.parametrize(
    "path", ["/me", "/profile", "/applications", "/consents", "/data-exports", "/privacy-requests"]
)
async def test_anonymous_is_401(public: httpx.AsyncClient, path: str) -> None:
    assert (await public.get(f"{PORTAL}{path}")).status_code == 401


async def test_existing_candidate_sign_in(app: FastAPI, site: Site) -> None:
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    client = await sign_in(app, site.slug, EMAIL)
    try:
        assert (await client.get(f"{PORTAL}/applications")).json() == []
    finally:
        await client.aclose()
