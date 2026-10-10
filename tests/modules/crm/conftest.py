# ruff: noqa: E501
"""Fixtures / helpers for the Epic 15 (Sales CRM) tests: a tenant with its default Legal Entity and a signed-in
Tenant Admin, members with the Sales role, isolated file storage and API helpers."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.files import StoredFile
from app.models.identity import Tenant
from app.services import legal_entities_service
from tests.epic3 import _isolated_files, _no_real_ai  # noqa: F401
from tests.modules.identity.conftest import Member, csrf, join, make_tenant

API = "/api/v1"


@dataclasses.dataclass
class CrmOrg:
    tenant: Tenant
    admin: Member
    entity_id: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id


async def _org(app: FastAPI, name: str) -> CrmOrg:
    tenant = await make_tenant(seat_limit=50)
    async with tenant_session(tenant.id) as db:
        entity_id = (await legal_entities_service.provision_default_entity(db, tenant)).id
    admin = await join(app, tenant, name=name)
    return CrmOrg(tenant=tenant, admin=admin, entity_id=entity_id)


@pytest.fixture
async def corg(app: FastAPI) -> AsyncIterator[CrmOrg]:
    org = await _org(app, "Sam Seller")
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_corg(app: FastAPI) -> AsyncIterator[CrmOrg]:
    org = await _org(app, "Olga Other")
    yield org
    await org.admin.client.aclose()


JoinAs = Callable[..., Awaitable[Member]]


@pytest.fixture
async def join_as(app: FastAPI, corg: CrmOrg) -> AsyncIterator[JoinAs]:
    members: list[Member] = []

    async def _join(*role_keys: str, name: str = "Pat Member") -> Member:
        member = await join(app, corg.tenant, *role_keys, name=name)
        members.append(member)
        return member

    yield _join
    for m in members:
        await m.client.aclose()


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


async def ok(resp: httpx.Response, code: int = 200) -> Any:
    assert resp.status_code == code, resp.text
    return resp.json()


def err(resp: httpx.Response) -> tuple[int, str]:
    return resp.status_code, resp.json()["error"]["code"]


async def create_client(m: Member, name: str, **extra: Any) -> dict[str, Any]:
    return await ok(await m.client.post(f"{API}/clients", json={"name": name, **extra}, headers=h(m)), 201)


async def create_lead(m: Member, company: str, **extra: Any) -> dict[str, Any]:
    return await ok(await m.client.post(f"{API}/leads", json={"company_name": company, **extra}, headers=h(m)), 201)


async def transition_lead(m: Member, lead_id: str, to: str, **extra: Any) -> httpx.Response:
    return await m.client.post(f"{API}/leads/{lead_id}/transition", json={"to": to, **extra}, headers=h(m))


async def create_opp(m: Member, client_id: str, name: str = "Deal", **extra: Any) -> dict[str, Any]:
    body = {"name": name, "client_id": client_id, "estimated_revenue": "100000.00", **extra}
    return await ok(await m.client.post(f"{API}/opportunities", json=body, headers=h(m)), 201)


async def move(m: Member, opp: dict[str, Any], to: str, **extra: Any) -> httpx.Response:
    return await m.client.post(
        f"{API}/opportunities/{opp['id']}/transition",
        json={"to": to, "expected_stage": extra.pop("expected_stage", opp["stage"]), **extra},
        headers=h(m),
    )


async def stored_pdf(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        f = StoredFile(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            storage_key=f"t/{tenant_id}/2026/10/{uuid.uuid4().hex}",
            original_name="msa.pdf",
            content_type="application/pdf",
            size_bytes=10,
            sha256="0" * 64,
            scan_status="clean",
        )
        db.add(f)
        return f.id
