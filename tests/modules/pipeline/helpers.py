"""Shared fixtures / helpers for the Epic 6 pipeline tests.

``porg`` is a tenant with a signed-in Tenant Admin, a client with one contact (email) and an Open demand
(contract, INR per hour, submission limit 5). Candidates are created through the API; a "parsed" resume is
uploaded through the API and flagged parsed in the tenant session (the AI parse itself is Epic 3's).
"""

import dataclasses
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core import rate_limit
from app.core.database import tenant_session
from app.core.mailer import OutboxMailer, get_mailer
from app.core.settings import get_settings
from app.models.identity import Tenant
from tests.epic3 import resume_pdf
from tests.modules.demands.helpers import create_client, open_demand
from tests.modules.identity.conftest import Member, csrf, join, make_tenant, new_client

API = "/api/v1"
LINK_RE = re.compile(r"/l/([A-Za-z0-9_\-]{20,})")


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


@dataclasses.dataclass
class POrg:
    tenant: Tenant
    admin: Member
    client_id: uuid.UUID
    contact_id: uuid.UUID
    contact_email: str
    demand: dict[str, Any]

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def demand_id(self) -> str:
        return str(self.demand["id"])


async def create_contact(m: Member, client_id: uuid.UUID, email: str | None = None) -> tuple[uuid.UUID, str]:
    email = email or f"hm-{uuid.uuid4().hex[:8]}@client.example.com"
    resp = await m.client.post(
        f"{API}/clients/{client_id}/contacts",
        json={"name": "Hema Manager", "email": email, "type": "hiring_manager"},
        headers=h(m),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"]), email


async def _porg(app: FastAPI, name: str, **demand: Any) -> POrg:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name=name)
    client_id = await create_client(admin)
    contact_id, email = await create_contact(admin, client_id)
    body = {"submission_limit": 5, **demand}
    d = await open_demand(admin, client_id, **body)
    return POrg(
        tenant=tenant, admin=admin, client_id=client_id, contact_id=contact_id, contact_email=email, demand=d
    )


@pytest.fixture
async def porg(app: FastAPI) -> AsyncIterator[POrg]:
    org = await _porg(app, "Ada Admin")
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_porg(app: FastAPI) -> AsyncIterator[POrg]:
    org = await _porg(app, "Olga Other")
    yield org
    await org.admin.client.aclose()


@pytest.fixture(autouse=True)
def _fresh_limits() -> Iterator[None]:
    rate_limit.set_backend(rate_limit.memory_backend())
    rate_limit.memory_backend().reset()
    yield
    rate_limit.set_backend(None)
    rate_limit.memory_backend().reset()


@pytest.fixture
def outbox() -> OutboxMailer:
    mailer = get_mailer()
    assert isinstance(mailer, OutboxMailer)
    mailer.sent.clear()
    return mailer


@pytest.fixture
async def public(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an anonymous client (no cookies) sending the frontend ``Origin`` on every request."""
    async with new_client(app) as ac:
        ac.headers["Origin"] = get_settings().frontend_url
        yield ac


async def create_candidate(m: Member, name: str = "Priya Raman", **extra: Any) -> dict[str, Any]:
    tag = uuid.uuid4().hex[:6]
    body: dict[str, Any] = {
        "full_name": name,
        "emails": [{"email": f"priya.{tag}@example.com", "is_primary": True}],
        "phones": [{"phone": "+91 98765 43210", "is_primary": True}],
        "links": [{"url": f"https://www.linkedin.com/in/priya-{tag}"}],
        "current_title": "Data Engineer",
        "current_employer": "Infosys",
        "total_experience_months": 62,
        "notice_period_days": 30,
        "location": {"city": "Chennai", "country": "India"},
        "employment": [
            {
                "employer": "Infosys",
                "title": "Data Engineer",
                "is_current": True,
                "description": f"Built lakehouse pipelines. Contact priya.{tag}@example.com or 9876543210.",
            }
        ],
        **extra,
    }
    resp = await m.client.post(f"{API}/candidates", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def upload_parsed_resume(
    m: Member, tenant_id: uuid.UUID, candidate_id: str, *, parsed: bool = True
) -> None:
    resp = await m.client.post(
        f"{API}/candidates/{candidate_id}/resumes",
        files={"file": ("cv.pdf", resume_pdf(), "application/octet-stream")},
        headers=h(m),
    )
    assert resp.status_code == 201, resp.text
    if parsed:
        from app.models.candidates import CandidateResume

        async with tenant_session(tenant_id) as db:
            await db.execute(
                update(CandidateResume)
                .where(CandidateResume.candidate_id == uuid.UUID(candidate_id))
                .values(parse_status="parsed")
            )


async def add(m: Member, demand_id: str, *candidate_ids: str, source: str = "manual") -> httpx.Response:
    return await m.client.post(
        f"{API}/demands/{demand_id}/applications",
        json={"candidate_ids": list(candidate_ids), "source": source},
        headers=h(m),
    )


async def add_one(m: Member, demand_id: str, candidate_id: str) -> str:
    resp = await add(m, demand_id, candidate_id)
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["status"] == "created", item
    return str(item["application_id"])


async def stages(m: Member) -> dict[str, str]:
    resp = await m.client.get(f"{API}/pipeline/stages")
    assert resp.status_code == 200, resp.text
    return {s["key"]: s["id"] for s in resp.json()["items"]}


async def move(m: Member, app_id: str, stage_id: str, **body: Any) -> httpx.Response:
    return await m.client.post(
        f"{API}/applications/{app_id}/transition", json={"to_stage_id": stage_id, **body}, headers=h(m)
    )


async def shortlist(m: Member, app_id: str) -> None:
    ids = await stages(m)
    resp = await move(m, app_id, ids["shortlisted"])
    assert resp.status_code == 200, resp.text


async def stage_of(m: Member, app_id: str) -> str:
    resp = await m.client.get(f"{API}/applications/{app_id}")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["stage"]["key"])


def per_app(
    pay: str = "900.00", bill: str = "1500.00", currency: str = "INR", **extra: Any
) -> dict[str, Any]:
    return {
        "pay_rate": {"amount": pay, "currency": currency},
        "bill_rate": {"amount": bill, "currency": currency},
        "resume_kind": "original",
        **extra,
    }


async def submit(
    org: POrg,
    app_ids: list[str],
    per: dict[str, dict[str, Any]] | None = None,
    member: Member | None = None,
    **body: Any,
) -> httpx.Response:
    m = member or org.admin
    payload = {
        "application_ids": app_ids,
        "per_application": per
        or {
            a: per_app(consent_attestation={"text": "Candidate agreed by phone on 1 Oct", "confirmed": True})
            for a in app_ids
        },
        "recipient_contact_ids": [str(org.contact_id)],
        "note": "Strong PySpark background.",
        **body,
    }
    return await m.client.post(f"{API}/demands/{org.demand_id}/submissions", json=payload, headers=h(m))


def links_in(outbox: OutboxMailer, kind: str) -> list[str]:
    tokens: list[str] = []
    for msg in outbox.sent:
        if msg.kind == kind:
            tokens += LINK_RE.findall((msg.html or "") + "\n" + msg.text)
    return list(dict.fromkeys(tokens))


async def rows(tenant_id: uuid.UUID, stmt: Any) -> list[Any]:
    async with tenant_session(tenant_id) as db:
        return list((await db.execute(stmt)).scalars())


__all__ = ["select"]
