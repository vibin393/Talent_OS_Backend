"""Story 9.2: consent-first apply, bot challenge, scanned upload, parse gating, duplicate-safe submit,
identical responses, confirmation email with a portal link, purge of expired sessions."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core import bot_challenge, scanning
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.ai import AIAction
from app.models.candidates import Candidate, CandidateEmail, CandidateResume
from app.models.files import StoredFile
from app.models.pipeline import Application
from app.models.portal import ApplicationAnswer, ApplySession
from app.models.privacy import Consent
from tests.epic3 import FakeAI, create_candidate
from tests.epic12 import sent
from tests.portal.conftest import Site, apply, careers, notices, origin, publish, start, submit, upload


async def _apps(s: Site) -> list[Application]:
    async with tenant_session(s.tenant_id) as db:
        return list((await db.execute(select(Application))).scalars())


async def _candidate_by_email(s: Site, email: str) -> Candidate | None:
    async with tenant_session(s.tenant_id) as db:
        return (
            await db.execute(
                select(Candidate)
                .join(CandidateEmail, CandidateEmail.candidate_id == Candidate.id)
                .where(CandidateEmail.email == email)
            )
        ).scalar_one_or_none()


async def test_full_apply_with_ai_consent(site: Site, public: httpx.AsyncClient, fake_ai: FakeAI) -> None:
    p = await publish(
        site, questions=[{"key": "notice", "label": "Notice (days)?", "type": "number", "required": True}]
    )
    r = await start(public, site.slug, p["posting_id"], ai=True)
    assert r.status_code == 201, r.text
    token = r.json()["apply_token"]
    u = await upload(public, site.slug, token)
    assert u.status_code == 200, u.text
    body = u.json()
    assert body["parse_status"] == "parsed"
    assert (
        body["prefill"]["email"] == "priya.raman@example.com"
        and body["prefill"]["full_name"] == "Priya Raman"
    )
    assert len(fake_ai.calls) == 1

    missing = await submit(public, site.slug, token)
    assert (missing.status_code, missing.json()["error"]["code"]) == (422, "answers_required")
    ok = await submit(public, site.slug, token, answers={"notice": 30}, phone="+91 98765 43210")
    assert (ok.status_code, ok.json()) == (202, {"status": "received"})

    apps = await _apps(site)
    assert len(apps) == 1 and apps[0].source == "career_portal" and apps[0].source_ref == p["posting_id"]
    cand = await _candidate_by_email(site, "priya.raman@example.com")
    assert cand is not None and cand.source == "career_portal"
    async with tenant_session(site.tenant_id) as db:
        consents = list((await db.execute(select(Consent).where(Consent.candidate_id == cand.id))).scalars())
        assert {c.purpose for c in consents} == {"data_processing", "ai_processing"}
        assert all(c.source == "application" for c in consents)
        action = (await db.execute(select(AIAction).where(AIAction.feature == "resume_parsing"))).scalar_one()
        assert all(c.granted_at < action.created_at for c in consents)  # consent before the parse
        assert action.subject_id == cand.id and action.decision in ("accepted", "edited")
        answers = (await db.execute(select(ApplicationAnswer))).scalar_one()
        assert answers.answers == {"notice": 30}
        session = (await db.execute(select(ApplySession))).scalar_one()
        assert (
            session.parsed_data is None and session.resume_text is None and session.submitted_at is not None
        )
    mail = sent(kind="apply_confirmation", to="priya.raman@example.com")
    assert mail and "We received your application for Senior Data Engineer" in mail[-1].subject
    assert "/portal/verify#t=" in mail[-1].text


async def test_no_ai_consent_never_calls_gateway(
    site: Site, public: httpx.AsyncClient, fake_ai: FakeAI
) -> None:
    p = await publish(site)
    r = await start(public, site.slug, p["posting_id"], ai=False)
    token = r.json()["apply_token"]
    u = await upload(public, site.slug, token)
    assert u.json() == {"parse_status": "skipped_consent", "prefill": {
        "full_name": None, "email": None, "phone": None, "location": None, "current_title": None,
        "current_employer": None, "total_experience_months": None, "skills": [], "links": []}}  # fmt: skip
    assert fake_ai.calls == []
    assert (await submit(public, site.slug, token)).status_code == 202
    assert fake_ai.calls == []


async def test_consent_and_notice_rules(site: Site, public: httpx.AsyncClient) -> None:
    p = await publish(site)
    ids = await notices(public, site.slug, p["posting_id"])
    url = f"{careers(site.slug)}/postings/{p['posting_id']}/apply-sessions"
    no_data = await public.post(
        url,
        json={"consents": [{"purpose": "ai_processing", "notice_id": ids["ai_processing"]}]},
        headers=origin(),
    )
    assert (no_data.status_code, no_data.json()["error"]["code"]) == (422, "consent_required")
    stale = await public.post(
        url,
        json={"consents": [{"purpose": "data_processing", "notice_id": str(uuid.uuid4())}]},
        headers=origin(),
    )
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "notice_outdated")
    foreign = await public.post(
        url, json={"consents": [{"purpose": "data_processing", "notice_id": ids["data_processing"]}]},
        headers={"Origin": "https://evil.example.com"},
    )  # fmt: skip
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (403, "origin_not_allowed")


class _Deny:
    async def verify(self, token: str | None, remote_ip: str) -> bool:
        return False


class _Down:
    async def verify(self, token: str | None, remote_ip: str) -> bool:
        raise TimeoutError


async def test_bot_challenge_fails_closed(site: Site, public: httpx.AsyncClient) -> None:
    p = await publish(site)
    for verifier in (_Deny(), _Down()):
        bot_challenge.set_verifier(verifier)
        r = await start(public, site.slug, p["posting_id"])
        assert (r.status_code, r.json()["error"]["code"]) == (422, "challenge_failed")


def test_production_refuses_noop_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "app_env", "production")
    monkeypatch.setattr(get_settings(), "bot_challenge_provider", "none")
    with pytest.raises(bot_challenge.ChallengeConfigError):
        bot_challenge.check_configuration()
    monkeypatch.setattr(get_settings(), "bot_challenge_provider", "turnstile")
    bot_challenge.check_configuration()
    assert isinstance(bot_challenge.get_verifier(), bot_challenge.TurnstileVerifier)


async def test_infected_resume_rejected(
    site: Site, public: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await publish(site)
    token = (await start(public, site.slug, p["posting_id"])).json()["apply_token"]

    async def infected(data: bytes, name: str) -> scanning.ScanResult:
        return scanning.ScanResult(status="infected", detail="Eicar")

    monkeypatch.setattr(scanning, "scan", infected)
    r = await upload(public, site.slug, token)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "file_infected")
    async with tenant_session(site.tenant_id) as db:
        assert (
            await db.execute(select(StoredFile).where(StoredFile.owner_entity_type == "apply_session"))
        ).first() is None
    no_resume = await submit(public, site.slug, token)
    assert (no_resume.status_code, no_resume.json()["error"]["code"]) == (422, "resume_required")


async def test_existing_email_links_without_overwrite(
    site: Site, public: httpx.AsyncClient, fake_ai: FakeAI
) -> None:
    existing = await create_candidate(
        site.admin,
        full_name="Priya R (recruiter entered)",
        emails=[{"email": "Priya.Raman@Example.com", "is_primary": True}],
    )
    p = await publish(site)
    resp = await apply(public, site.slug, p["posting_id"], ai=True, full_name="Someone Else")
    assert (resp.status_code, resp.json()) == (202, {"status": "received"})
    async with tenant_session(site.tenant_id) as db:
        cand = await db.get(Candidate, uuid.UUID(existing["id"]))
        assert cand is not None and cand.full_name == "Priya R (recruiter entered)"
        resumes = list(
            (
                await db.execute(select(CandidateResume).where(CandidateResume.candidate_id == cand.id))
            ).scalars()
        )
        assert len(resumes) == 1 and resumes[0].parsed_data is not None and resumes[0].version == 1
        assert (await db.execute(select(Candidate))).scalars().all().__len__() == 1
    apps = await _apps(site)
    assert len(apps) == 1 and str(apps[0].candidate_id) == existing["id"]


async def test_apply_twice_same_response_one_application(site: Site, public: httpx.AsyncClient) -> None:
    p = await publish(site)
    first = await apply(public, site.slug, p["posting_id"])
    second = await apply(public, site.slug, p["posting_id"])
    assert first.status_code == second.status_code == 202
    assert first.content == second.content
    assert len(await _apps(site)) == 1
    assert len(sent(kind="apply_confirmation", to="priya.raman@example.com")) >= 2


async def test_duplicate_review_items_never_merge(site: Site, public: httpx.AsyncClient) -> None:
    await create_candidate(
        site.admin, full_name="Priya Raman", phones=[{"phone": "+919876543210", "is_primary": True}]
    )
    p = await publish(site)
    resp = await apply(
        public, site.slug, p["posting_id"], email="priya.other@example.com", phone="+91 98765 43210"
    )
    assert resp.status_code == 202
    listed = await site.admin.client.get("/api/v1/candidates/duplicates", params={"status": "open"})
    if listed.status_code == 200:
        assert listed.json()["total"] >= 1
    async with tenant_session(site.tenant_id) as db:
        assert (
            len(
                list(
                    (
                        await db.execute(select(Candidate).where(Candidate.merged_into_id.is_not(None)))
                    ).scalars()
                )
            )
            == 0
        )


async def test_apply_session_rate_limit(
    site: Site, public: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    p = await publish(site)
    codes = [(await start(public, site.slug, p["posting_id"])).status_code for _ in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


async def test_upload_limit_and_replacement(site: Site, public: httpx.AsyncClient) -> None:
    p = await publish(site)
    token = (await start(public, site.slug, p["posting_id"])).json()["apply_token"]
    for _ in range(5):
        assert (await upload(public, site.slug, token)).status_code == 200
    assert (await upload(public, site.slug, token)).status_code == 429
    async with tenant_session(site.tenant_id) as db:
        files = list(
            (
                await db.execute(select(StoredFile).where(StoredFile.owner_entity_type == "apply_session"))
            ).scalars()
        )
        assert len(files) == 5 and sum(1 for f in files if f.deleted_at is None) == 1


async def test_bad_token_and_expired_session(site: Site, public: httpx.AsyncClient) -> None:
    p = await publish(site)
    assert (await upload(public, site.slug, "nope-" + "x" * 30)).status_code == 404
    token = (await start(public, site.slug, p["posting_id"])).json()["apply_token"]
    async with tenant_session(site.tenant_id) as db:
        await db.execute(update(ApplySession).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    r = await upload(public, site.slug, token)
    assert (r.status_code, r.json()["error"]["code"]) == (410, "apply_session_expired")


async def test_purge_deletes_expired_sessions_and_files(site: Site, public: httpx.AsyncClient) -> None:
    from app.core import storage
    from app.services import portal

    p = await publish(site)
    token = (await start(public, site.slug, p["posting_id"])).json()["apply_token"]
    assert (await upload(public, site.slug, token)).status_code == 200
    async with tenant_session(site.tenant_id) as db:
        row = (await db.execute(select(ApplySession))).scalar_one()
        file = await db.get(StoredFile, row.resume_file_id)
        assert file is not None
        key = file.storage_key
        await db.execute(update(ApplySession).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    counts = await portal.apply.purge_expired(site.tenant_id)
    assert counts == {"sessions": 1, "files": 1}
    async with tenant_session(site.tenant_id) as db:
        assert (await db.execute(select(ApplySession))).first() is None
        assert (await db.get(StoredFile, file.id)).deleted_at is not None  # type: ignore[union-attr]
    with pytest.raises(FileNotFoundError):
        await storage.get_storage().get(key)


async def test_apply_to_other_tenant_posting_404(
    site: Site, other_site: Site, public: httpx.AsyncClient
) -> None:
    p = await publish(site)
    ids = await notices(public, site.slug, p["posting_id"])
    r = await public.post(
        f"{careers(other_site.slug)}/postings/{p['posting_id']}/apply-sessions",
        json={"consents": [{"purpose": "data_processing", "notice_id": ids["data_processing"]}]},
        headers=origin(),
    )
    assert r.status_code == 404
    token = (await start(public, site.slug, p["posting_id"])).json()["apply_token"]
    assert (await upload(public, other_site.slug, token)).status_code == 404
    assert (await submit(public, other_site.slug, token)).status_code == 404
