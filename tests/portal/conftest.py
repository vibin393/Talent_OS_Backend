"""Shared fixtures / helpers for the Epic 9 portal tests (career site, apply, candidate portal, privacy)."""

import dataclasses
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core import bot_challenge, rate_limit
from app.core.settings import get_settings
from tests.epic3 import _isolated_files, _no_real_ai, fake_ai, resume_pdf  # noqa: F401
from tests.epic12 import sent
from tests.modules.demands.helpers import DEMANDS, DOrg, create_client, h, open_demand
from tests.modules.identity.conftest import Member, csrf, join, make_tenant, new_client

ADMIN_SITE = "/api/v1/admin/career-site"
PORTAL = "/api/v1/portal"
AUTH = "/api/v1/public/candidate-auth"


def careers(slug: str) -> str:
    return f"/api/v1/public/careers/{slug}"


def origin() -> dict[str, str]:
    return {"Origin": get_settings().frontend_url.rstrip("/")}


@pytest.fixture(autouse=True)
def _portal_env() -> Iterator[None]:
    rate_limit.memory_backend().reset()
    bot_challenge.set_verifier(None)
    yield
    bot_challenge.set_verifier(None)
    rate_limit.memory_backend().reset()


@dataclasses.dataclass
class Site:
    org: DOrg
    slug: str

    @property
    def admin(self) -> Member:
        return self.org.admin

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.org.tenant_id


async def _site(app: FastAPI, name: str, client_name: str) -> Site:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name=name)
    org = DOrg(tenant=tenant, admin=admin, client_id=await create_client(admin, client_name))
    resp = await admin.client.patch(
        ADMIN_SITE,
        json={
            "enabled": True,
            "display_name": f"{name} Careers",
            "brand_color": "#0F6CBD",
            "privacy_contact_email": "privacy@acme.example.com",
            "about_md": "We **hire** people. <script>alert(1)</script>",
        },
        headers=h(admin),
    )
    assert resp.status_code == 200, resp.text
    return Site(org=org, slug=str(tenant.slug))


@pytest.fixture
async def site(app: FastAPI) -> AsyncIterator[Site]:
    s = await _site(app, "Acme", "SENTINEL_CLIENT")
    yield s
    await s.admin.client.aclose()


@pytest.fixture
async def other_site(app: FastAPI) -> AsyncIterator[Site]:
    s = await _site(app, "Globex", "Other Client Ltd")
    yield s
    await s.admin.client.aclose()


@pytest.fixture
async def public(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    client = new_client(app)
    yield client
    await client.aclose()


JD_TEXT = (
    "## About the role\n\nBuild **data pipelines** with PySpark and Azure.\n\n<script>alert('x')</script>"
)


async def publish(
    s: Site,
    *,
    title: str = "Senior Data Engineer",
    show_client_name: bool = False,
    show_rate: bool = False,
    questions: list[dict[str, Any]] | None = None,
    **demand: Any,
) -> dict[str, Any]:
    """Open a demand, publish a posting JD, optionally set apply questions. Returns ids."""
    m = s.admin
    d = await open_demand(m, s.org.client_id, title=title, bill_rate="9999.00", **demand)
    v = await m.client.post(
        f"{DEMANDS}/{d['id']}/jd-versions", json={"kind": "posting", "content": JD_TEXT}, headers=h(m)
    )
    assert v.status_code == 201, v.text
    p = await m.client.post(
        f"{DEMANDS}/{d['id']}/posting",
        json={"jd_version_id": v.json()["id"], "show_client_name": show_client_name, "show_rate": show_rate},
        headers=h(m),
    )
    assert p.status_code == 200, p.text
    if questions is not None:
        q = await m.client.patch(
            f"{DEMANDS}/{d['id']}/posting", json={"apply_questions": questions}, headers=h(m)
        )
        assert q.status_code == 200, q.text
    return {"demand_id": d["id"], "posting_id": p.json()["id"], "req_id": d["req_id"]}


async def notices(client: httpx.AsyncClient, slug: str, posting_id: str) -> dict[str, str]:
    resp = await client.get(f"{careers(slug)}/postings/{posting_id}/apply-form")
    assert resp.status_code == 200, resp.text
    return {n["purpose"]: n["id"] for n in resp.json()["notices"]}


async def start(client: httpx.AsyncClient, slug: str, posting_id: str, *, ai: bool = False) -> httpx.Response:
    ids = await notices(client, slug, posting_id)
    consents = [{"purpose": "data_processing", "notice_id": ids["data_processing"]}]
    if ai:
        consents.append({"purpose": "ai_processing", "notice_id": ids["ai_processing"]})
    return await client.post(
        f"{careers(slug)}/postings/{posting_id}/apply-sessions",
        json={"consents": consents, "challenge_token": "ok"},
        headers=origin(),
    )


async def upload(
    client: httpx.AsyncClient, slug: str, token: str, data: bytes | None = None
) -> httpx.Response:
    return await client.post(
        f"{careers(slug)}/apply-sessions/resume",
        files={"file": ("cv.pdf", data if data is not None else resume_pdf(), "application/octet-stream")},
        headers={**origin(), "X-Apply-Token": token},
    )


async def submit(client: httpx.AsyncClient, slug: str, token: str, **body: Any) -> httpx.Response:
    payload = {
        "full_name": "Priya Raman",
        "email": "priya.raman@example.com",
        "links": [],
        "answers": {},
        **body,
    }
    return await client.post(
        f"{careers(slug)}/apply-sessions/submit", json=payload, headers={**origin(), "X-Apply-Token": token}
    )


async def apply(
    client: httpx.AsyncClient, slug: str, posting_id: str, *, ai: bool = False, **body: Any
) -> httpx.Response:
    r = await start(client, slug, posting_id, ai=ai)
    assert r.status_code == 201, r.text
    token = r.json()["apply_token"]
    u = await upload(client, slug, token)
    assert u.status_code == 200, u.text
    return await submit(client, slug, token, **body)


TOKEN_RE = re.compile(r"/portal/verify#t=([A-Za-z0-9_\-]+)")


def last_link(to: str, kind: str = "candidate_sign_in") -> str:
    messages = sent(kind=kind, to=to)
    assert messages, f"no {kind} email to {to}"
    match = TOKEN_RE.search(messages[-1].text)
    assert match, messages[-1].text
    return match.group(1)


async def sign_in(app: FastAPI, slug: str, email: str) -> httpx.AsyncClient:
    """Request a link, follow it, return a client carrying ``tos_candidate`` + ``tos_csrf``."""
    client = new_client(app)
    r = await client.post(f"{AUTH}/request", json={"tenant_slug": slug, "email": email})
    assert r.status_code == 202, r.text
    token = last_link(email)
    v = await client.post(f"{AUTH}/verify", json={"token": token})
    assert v.status_code == 200, v.text
    assert client.cookies.get("tos_candidate")
    return client


def ch(client: httpx.AsyncClient) -> dict[str, str]:
    return csrf(client)
