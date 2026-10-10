"""Story 9.4 (FR-51): consents (withdraw, re-grant, AI gateway honours withdrawal), data export (DSR + job,
409 while running, zip via portal and secure link), correction / erasure requests (DSR open, due +30 d,
nothing deleted, admin list)."""

import io
import json
import zipfile
from datetime import timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.candidates import Candidate
from app.models.portal import DataExportJob
from app.models.privacy import DsrRequest
from app.services import ai
from tests.epic3 import FakeAI, resume_pdf
from tests.epic12 import sent
from tests.modules.identity.conftest import new_client
from tests.portal.conftest import PORTAL, Site, apply, ch, origin, publish, sign_in

EMAIL = "priya.raman@example.com"
LINK_RE = r"/l/([A-Za-z0-9_\-]+)"


async def _signed_in(
    app: FastAPI, site: Site, public: httpx.AsyncClient, *, ai_consent: bool = False
) -> httpx.AsyncClient:
    p = await publish(site)
    assert (await apply(public, site.slug, p["posting_id"], ai=ai_consent)).status_code == 202
    return await sign_in(app, site.slug, EMAIL)


async def test_consents_withdraw_regrant_and_gateway(
    app: FastAPI, site: Site, public: httpx.AsyncClient, fake_ai: FakeAI
) -> None:
    ai.set_consent_checker(None)  # the real privacy checker
    client = await _signed_in(app, site, public, ai_consent=True)
    try:
        consents = (await client.get(f"{PORTAL}/consents")).json()
        by_purpose = {c["purpose"]: c for c in consents}
        assert set(by_purpose) == {"data_processing", "ai_processing"}
        assert (
            by_purpose["ai_processing"]["notice_version"] == 1
            and by_purpose["ai_processing"]["client_name"] is None
        )
        calls = len(fake_ai.calls)
        w = await client.post(
            f"{PORTAL}/consents/{by_purpose['ai_processing']['id']}/withdraw", headers=ch(client)
        )
        assert w.status_code == 200, w.text
        assert (
            w.json()["consent"]["status"] == "withdrawn" and w.json()["details"]["suggest_erasure"] is False
        )
        # The next AI call for this candidate is skipped (resume upload -> no parse).
        up = await client.post(
            f"{PORTAL}/resumes",
            files={"file": ("cv.pdf", resume_pdf(), "application/pdf")},
            headers=ch(client),
        )
        assert up.status_code == 201
        assert len(fake_ai.calls) == calls
        again = await client.post(
            f"{PORTAL}/consents/{by_purpose['ai_processing']['id']}/withdraw", headers=ch(client)
        )
        assert (again.status_code, again.json()["error"]["code"]) == (409, "consent_not_active")

        form = (await public.get(f"/api/v1/public/careers/{site.slug}/postings")).json()
        assert form["total"] == 1
        notices = {
            n["purpose"]: n["id"]
            for n in (
                await public.get(
                    f"/api/v1/public/careers/{site.slug}/postings/{form['items'][0]['id']}/apply-form"
                )
            ).json()["notices"]
        }
        stale = await client.post(
            f"{PORTAL}/consents",
            json={"purpose": "marketing", "notice_id": notices["ai_processing"]},
            headers=ch(client),
        )
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "notice_outdated")
        regrant = await client.post(
            f"{PORTAL}/consents",
            json={"purpose": "ai_processing", "notice_id": notices["ai_processing"]},
            headers=ch(client),
        )
        assert regrant.status_code == 201 and regrant.json()["status"] == "granted"
        data = next(c for c in consents if c["purpose"] == "data_processing")
        dp = await client.post(f"{PORTAL}/consents/{data['id']}/withdraw", headers=ch(client))
        assert dp.json()["details"]["suggest_erasure"] is True
    finally:
        await client.aclose()


async def test_data_export_flow(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    client = await _signed_in(app, site, public)
    try:
        r = await client.post(f"{PORTAL}/data-exports", headers=ch(client))
        assert r.status_code == 202, r.text
        # Eager Celery: the job already ran after commit.
        jobs = (await client.get(f"{PORTAL}/data-exports")).json()
        assert jobs[0]["status"] == "ready", jobs
        zipped = await client.get(jobs[0]["download_url"])
        assert zipped.status_code == 200
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as zf:
            names = zf.namelist()
            data = json.loads(zf.read("data.json"))
        assert "data.json" in names and any(n.startswith("files/") for n in names)
        assert data["profile"]["full_name"] == "Priya Raman"
        assert data["applications"][0]["stage_label"] == "Application received"
        async with tenant_session(site.tenant_id) as db:
            dsr = (await db.execute(select(DsrRequest).where(DsrRequest.kind == "access"))).scalar_one()
            assert dsr.status == "completed" and dsr.channel == "candidate_portal"

        mail = sent(kind="data_export_ready", to=EMAIL)
        assert mail
        import re

        token = re.search(LINK_RE, mail[-1].text).group(1)  # type: ignore[union-attr]
        anon = new_client(app)
        try:
            view = await anon.post(f"/api/v1/public/links/{token}/download", headers=origin())
            assert view.status_code == 200 and view.content[:2] == b"PK"
            foreign = await anon.post(
                f"/api/v1/public/links/{token}/download", headers={"Origin": "https://evil.example"}
            )
            assert foreign.status_code == 403
        finally:
            await anon.aclose()
    finally:
        await client.aclose()


async def test_second_export_while_running_409(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    import uuid

    from app.services.portal.common import now

    client = await _signed_in(app, site, public)
    try:
        async with tenant_session(site.tenant_id) as db:
            cand = (await db.execute(select(Candidate))).scalar_one()
            db.add(
                DataExportJob(
                    id=uuid.uuid4(),
                    tenant_id=site.tenant_id,
                    candidate_id=cand.id,
                    status="running",
                    requested_at=now(),
                )
            )
        second = await client.post(f"{PORTAL}/data-exports", headers=ch(client))
        assert (second.status_code, second.json()["error"]["code"]) == (409, "export_in_progress")
    finally:
        await client.aclose()


async def test_erasure_request_opens_dsr_without_deleting(
    app: FastAPI, site: Site, public: httpx.AsyncClient
) -> None:
    client = await _signed_in(app, site, public)
    try:
        r = await client.post(
            f"{PORTAL}/privacy-requests",
            json={"kind": "erasure", "details": "Please delete my data"},
            headers=ch(client),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "open" and body["kind"] == "erasure"
        from datetime import datetime

        received = datetime.fromisoformat(body["received_at"])
        due = datetime.fromisoformat(body["due_at"])
        assert due - received == timedelta(days=30)
        c = await client.post(
            f"{PORTAL}/privacy-requests",
            json={"kind": "correction", "details": "My email changed", "fields": ["emails"]},
            headers=ch(client),
        )
        assert c.status_code == 201
        listed = (await client.get(f"{PORTAL}/privacy-requests")).json()
        assert {x["kind"] for x in listed} == {"erasure", "correction"}
        async with tenant_session(site.tenant_id) as db:
            cand = (await db.execute(select(Candidate))).scalar_one()
            assert cand.erased_at is None and cand.full_name == "Priya Raman"
        admin = await site.admin.client.get("/api/v1/privacy/dsr-requests", params={"kind": "erasure"})
        assert admin.status_code == 200 and admin.json()["total"] == 1
        assert sent(kind="dsr_portal_request", to="privacy@acme.example.com")
    finally:
        await client.aclose()


async def test_export_job_rows_scoped(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    client = await _signed_in(app, site, public)
    try:
        assert (await client.post(f"{PORTAL}/data-exports", headers=ch(client))).status_code == 202
        async with tenant_session(site.tenant_id) as db:
            job = (await db.execute(select(DataExportJob))).scalar_one()
            assert job.storage_key is not None and job.storage_key.startswith(f"t/{site.tenant_id}/exports/")
    finally:
        await client.aclose()
