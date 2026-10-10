"""Story 9.3: magic-link sign-in (identical responses, limits, single use, expiry), candidate sessions, and
principal separation (``tos_session`` vs ``tos_candidate``)."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from freezegun import freeze_time
from sqlalchemy import update

from app.core.database import platform_session
from app.core.settings import get_settings
from app.models.identity import Session as AuthSession
from tests.epic3 import create_candidate
from tests.epic12 import sent
from tests.modules.identity.conftest import new_client
from tests.portal.conftest import AUTH, PORTAL, Site, apply, ch, last_link, publish, sign_in

EMAIL = "priya.raman@example.com"


async def test_known_and_unknown_emails_get_identical_202(site: Site, public: httpx.AsyncClient) -> None:
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    before = len(sent(kind="candidate_sign_in"))
    known = await public.post(f"{AUTH}/request", json={"tenant_slug": site.slug, "email": EMAIL})
    unknown = await public.post(
        f"{AUTH}/request", json={"tenant_slug": site.slug, "email": "nobody@example.com"}
    )
    no_tenant = await public.post(f"{AUTH}/request", json={"tenant_slug": "no-such-x", "email": EMAIL})
    assert known.status_code == unknown.status_code == no_tenant.status_code == 202
    assert known.content == unknown.content == no_tenant.content == b'{"status":"check_email"}'
    after = sent(kind="candidate_sign_in")
    assert len(after) == before + 1 and after[-1].to == EMAIL
    assert "/portal/verify#t=" in after[-1].text


async def test_sixth_request_per_hour_silently_dropped(
    site: Site, public: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    before = len(sent(kind="candidate_sign_in", to=EMAIL))
    responses = [
        await public.post(f"{AUTH}/request", json={"tenant_slug": site.slug, "email": EMAIL})
        for _ in range(6)
    ]
    assert {r.status_code for r in responses} == {202}
    assert len({r.content for r in responses}) == 1
    assert len(sent(kind="candidate_sign_in", to=EMAIL)) == before + 5


async def test_ip_limit_429(site: Site, public: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr("app.services.portal.candidate_auth.IP_LIMIT", 2)
    monkeypatch.setattr("app.services.portal.candidate_auth.MIN_RESPONSE_SECONDS", 0)
    codes = [
        (
            await public.post(
                f"{AUTH}/request", json={"tenant_slug": site.slug, "email": f"x{i}@example.com"}
            )
        ).status_code
        for i in range(3)
    ]
    assert codes == [202, 202, 429]


async def test_verify_sets_cookie_single_use_and_old_links_revoked(app: FastAPI, site: Site) -> None:
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    client = new_client(app)
    try:
        await client.post(f"{AUTH}/request", json={"tenant_slug": site.slug, "email": EMAIL})
        first = last_link(EMAIL)
        await client.post(f"{AUTH}/request", json={"tenant_slug": site.slug, "email": EMAIL})
        second = last_link(EMAIL)
        revoked = await client.post(f"{AUTH}/verify", json={"token": first})
        assert (revoked.status_code, revoked.json()["error"]["code"]) == (410, "link_revoked")
        ok = await client.post(f"{AUTH}/verify", json={"token": second})
        assert ok.status_code == 200, ok.text
        assert ok.json()["candidate"]["first_name"] == "Priya"
        assert ok.json()["tenant"]["slug"] == site.slug
        cookie = ok.headers.get("set-cookie") or ""
        assert (
            "tos_candidate=" in cookie and "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
        )
        used = await client.post(f"{AUTH}/verify", json={"token": second})
        assert (used.status_code, used.json()["error"]["code"]) == (410, "link_used")
        invalid = await client.post(f"{AUTH}/verify", json={"token": "x" * 43})
        assert (invalid.status_code, invalid.json()["error"]["code"]) == (404, "link_invalid")
        me = await client.get(f"{PORTAL}/me")
        assert me.status_code == 200 and me.json()["tenant"]["display_name"] == "Acme Careers"
    finally:
        await client.aclose()


async def test_parallel_verify_exactly_one_success(app: FastAPI, site: Site) -> None:
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    requester = new_client(app)
    await requester.post(f"{AUTH}/request", json={"tenant_slug": site.slug, "email": EMAIL})
    token = last_link(EMAIL)
    clients = [new_client(app) for _ in range(4)]
    try:
        results = await asyncio.gather(*(c.post(f"{AUTH}/verify", json={"token": token}) for c in clients))
        codes = sorted(r.status_code for r in results)
        assert codes.count(200) == 1 and all(c == 410 for c in codes if c != 200)
    finally:
        for c in [requester, *clients]:
            await c.aclose()


async def test_link_expires_after_15_minutes(app: FastAPI, site: Site) -> None:
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    client = new_client(app)
    try:
        await client.post(f"{AUTH}/request", json={"tenant_slug": site.slug, "email": EMAIL})
        token = last_link(EMAIL)
        with freeze_time(datetime.now(UTC) + timedelta(minutes=16)):
            late = await client.post(f"{AUTH}/verify", json={"token": token})
        assert (late.status_code, late.json()["error"]["code"]) == (410, "link_expired")
    finally:
        await client.aclose()


async def test_confirmation_link_signs_in_and_shows_application(
    app: FastAPI, site: Site, public: httpx.AsyncClient
) -> None:
    p = await publish(site)
    assert (await apply(public, site.slug, p["posting_id"])).status_code == 202
    token = last_link(EMAIL, kind="apply_confirmation")
    client = new_client(app)
    try:
        assert (await client.post(f"{AUTH}/verify", json={"token": token})).status_code == 200
        apps = (await client.get(f"{PORTAL}/applications")).json()
        assert [a["stage_label"] for a in apps] == ["Application received"]
    finally:
        await client.aclose()


async def test_principals_do_not_cross(app: FastAPI, site: Site) -> None:
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    # A user's tos_session is rejected on /portal/*.
    assert (await site.admin.client.get(f"{PORTAL}/me")).status_code == 401
    candidate = await sign_in(app, site.slug, EMAIL)
    try:
        # tos_candidate is rejected on internal routers (user auth reads only tos_session).
        assert (await candidate.get("/api/v1/candidates")).status_code == 401
        assert (await candidate.get("/api/v1/auth/me")).status_code == 401
        # CSRF on portal mutations.
        no_csrf = await candidate.patch(f"{PORTAL}/profile", json={"headline": "x"})
        assert (no_csrf.status_code, no_csrf.json()["error"]["code"]) == (403, "csrf_failed")
        out = await candidate.post(f"{PORTAL}/auth/logout", headers=ch(candidate))
        assert out.status_code == 204
        assert (await candidate.get(f"{PORTAL}/me")).status_code == 401
    finally:
        await candidate.aclose()


async def test_idle_session_expires(app: FastAPI, site: Site) -> None:
    await create_candidate(site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}])
    candidate = await sign_in(app, site.slug, EMAIL)
    try:
        async with platform_session() as db:
            await db.execute(
                update(AuthSession)
                .where(AuthSession.principal_type == "candidate")
                .where(AuthSession.tenant_id == site.tenant_id)
                .values(last_seen_at=datetime.now(UTC) - timedelta(hours=25))
            )
        assert (await candidate.get(f"{PORTAL}/me")).status_code == 401
    finally:
        await candidate.aclose()


async def test_merged_candidate_gets_no_link(site: Site, public: httpx.AsyncClient) -> None:
    from app.core.database import tenant_session
    from app.models.candidates import Candidate

    c = await create_candidate(
        site.admin, full_name="Priya Raman", emails=[{"email": EMAIL, "is_primary": True}]
    )
    keeper = await create_candidate(site.admin, full_name="Priya R")
    async with tenant_session(site.tenant_id) as db:
        await db.execute(update(Candidate).where(Candidate.id == c["id"]).values(merged_into_id=keeper["id"]))
    before = len(sent(kind="candidate_sign_in", to=EMAIL))
    r = await public.post(f"{AUTH}/request", json={"tenant_slug": site.slug, "email": EMAIL})
    assert r.status_code == 202
    assert len(sent(kind="candidate_sign_in", to=EMAIL)) == before
