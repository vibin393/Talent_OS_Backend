"""Shared fixtures / helpers for the Epic 4 tests (demands, approvals)."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.permissions import CATALOG, Principal, Scope
from app.models.identity import Tenant
from tests.modules.identity.conftest import Member, csrf, join, make_tenant

DEMANDS = "/api/v1/demands"
CLIENTS = "/api/v1/clients"
CHAINS = "/api/v1/approval-chains"
APPROVALS = "/api/v1/approvals"


@dataclasses.dataclass
class DOrg:
    tenant: Tenant
    admin: Member
    client_id: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


async def create_client(m: Member, name: str | None = None) -> uuid.UUID:
    resp = await m.client.post(CLIENTS, json={"name": name or f"Client {uuid.uuid4().hex[:8]}"}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _org(app: FastAPI, name: str) -> DOrg:
    tenant = await make_tenant(seat_limit=50)
    admin = await join(app, tenant, name=name)
    return DOrg(tenant=tenant, admin=admin, client_id=await create_client(admin))


@pytest.fixture
async def dorg(app: FastAPI) -> AsyncIterator[DOrg]:
    org = await _org(app, "Ada Admin")
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_dorg(app: FastAPI) -> AsyncIterator[DOrg]:
    org = await _org(app, "Olga Other")
    yield org
    await org.admin.client.aclose()


def ready_body(client_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Senior Data Engineer",
        "client_id": str(client_id),
        "positions": 2,
        "employment_type": "contract",
        "country": "IN",
        "location": "Chennai",
        "work_mode": "hybrid",
        "priority": "high",
        "bill_rate": "1500.00",
        "pay_rate_min": "900.00",
        "pay_rate_max": "1100.00",
        "currency": "INR",
        "rate_unit": "hour",
        "mandatory_skills": ["PySpark"],
    }
    body.update(overrides)
    return body


async def create_demand(m: Member, client_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    resp = await m.client.post(DEMANDS, json=ready_body(client_id, **overrides), headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def open_demand(m: Member, client_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    demand = await create_demand(m, client_id, **overrides)
    resp = await m.client.post(f"{DEMANDS}/{demand['id']}/submit", headers=h(m))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open", resp.json()
    return dict(resp.json())


def admin_ctx(org: DOrg, user_id: uuid.UUID | None = None) -> RequestContext:
    principal = Principal(
        type="user",
        id=user_id or org.admin.user_id,
        tenant_id=org.tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


async def role_id(org: DOrg, key: str) -> uuid.UUID:
    resp = await org.admin.client.get("/api/v1/admin/roles")
    assert resp.status_code == 200, resp.text
    return uuid.UUID(next(r["id"] for r in resp.json() if r["key"] == key))
