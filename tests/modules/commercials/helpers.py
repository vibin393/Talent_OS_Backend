"""Shared fixtures / helpers for the Epic 16 commercials tests."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.models.identity import Tenant
from tests.modules.demands.helpers import create_client
from tests.modules.identity.conftest import Member, csrf, join, make_tenant
from tests.modules.legal_entities.conftest import gstin, provision

API = "/api/v1"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


@dataclasses.dataclass
class COrg:
    tenant: Tenant
    admin: Member
    client_id: uuid.UUID
    entity_id: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def c(self) -> httpx.AsyncClient:
        return self.admin.client

    def h(self) -> dict[str, str]:
        return csrf(self.admin.client)

    def ctx(self) -> RequestContext:
        principal = Principal(
            type="user",
            id=self.admin.user_id,
            tenant_id=self.tenant_id,
            permissions=dict.fromkeys(CATALOG, Scope.TENANT),
        )
        return RequestContext(tenant_id=self.tenant_id, principal=principal)


async def _org(app: FastAPI, name: str) -> COrg:
    tenant = await make_tenant(seat_limit=50)
    entity_id = await provision(tenant)
    admin = await join(app, tenant, name=name)
    return COrg(tenant=tenant, admin=admin, client_id=await create_client(admin), entity_id=entity_id)


@pytest.fixture
async def corg(app: FastAPI) -> AsyncIterator[COrg]:
    org = await _org(app, "Ada Admin")
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_corg(app: FastAPI) -> AsyncIterator[COrg]:
    org = await _org(app, "Olga Other")
    yield org
    await org.admin.client.aclose()


async def ok(resp: httpx.Response, code: int = 200) -> Any:
    assert resp.status_code == code, resp.text
    return resp.json() if resp.content else None


def err(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code, body
    return dict(body["error"])


async def admin_approves_exceptions(org: COrg) -> None:
    """Route rate / PO exceptions to the signed-in admin (the default Finance Admin role is not held in tests)."""  # noqa: E501
    step = [{"name": "Finance", "approver_kind": "user", "approver_ref": {"user_id": str(org.admin.user_id)}}]
    out = await ok(
        await org.c.patch(
            f"{API}/commercials/settings",
            json={"rate_exception_steps": step, "po_exception_steps": step},
            headers=org.h(),
        )
    )
    assert out["rate_exception_steps"][0]["approver_kind"] == "user"


# ---------------------------------------------------------------- contracts
def contract_body(org: COrg, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract_type": "msa",
        "client_id": str(org.client_id),
        "legal_entity_id": str(org.entity_id),
        "title": "Master services agreement",
        "start_date": "2026-01-01",
        "end_date": "2027-12-31",
        "currency": "INR",
    }
    body.update(overrides)
    return body


async def create_contract(org: COrg, **overrides: Any) -> dict[str, Any]:
    return dict(
        await ok(
            await org.c.post(f"{API}/contracts", json=contract_body(org, **overrides), headers=org.h()), 201
        )
    )


async def upload_signed(org: COrg, contract_id: str) -> dict[str, Any]:
    resp = await org.c.post(
        f"{API}/contracts/{contract_id}/documents",
        files={"file": ("signed.pdf", PDF, "application/pdf")},
        data={"kind": "signed"},
        headers=org.h(),
    )
    return dict(await ok(resp, 201))


async def active_contract(org: COrg, **overrides: Any) -> dict[str, Any]:
    c = await create_contract(org, **overrides)
    await upload_signed(org, c["id"])
    return dict(await ok(await org.c.post(f"{API}/contracts/{c['id']}/activate", headers=org.h())))


# ---------------------------------------------------------------- rate cards
def line(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "role": "Java Developer",
        "location": "Bengaluru",
        "experience_min_years": "3",
        "experience_max_years": "6",
        "min_bill_rate": "1200",
        "max_bill_rate": "1800",
        "currency": "INR",
        "rate_unit": "hour",
    }
    body.update(overrides)
    return body


async def published_card(
    org: COrg,
    lines: list[dict[str, Any]] | None = None,
    effective_from: str = "2026-01-01",
    contract_id: str | None = None,
) -> dict[str, Any]:
    card = await ok(
        await org.c.post(
            f"{API}/rate-cards",
            json={
                "client_id": str(org.client_id),
                "name": "Standard rates",
                "effective_from": effective_from,
                "contract_id": contract_id,
            },
            headers=org.h(),
        ),
        201,
    )
    await ok(
        await org.c.put(
            f"{API}/rate-cards/{card['id']}/lines", json={"lines": lines or [line()]}, headers=org.h()
        )
    )
    return dict(await ok(await org.c.post(f"{API}/rate-cards/{card['id']}/publish", headers=org.h())))


# ---------------------------------------------------------------- purchase orders
def po_body(org: COrg, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_id": str(org.client_id),
        "legal_entity_id": str(org.entity_id),
        "po_number": f"PO-{uuid.uuid4().hex[:6]}",
        "currency": "INR",
        "value_cap": "500000.00",
        "valid_from": "2026-01-01",
        "valid_to": "2027-12-31",
    }
    body.update(overrides)
    return body


async def create_po(org: COrg, **overrides: Any) -> dict[str, Any]:
    return dict(
        await ok(
            await org.c.post(f"{API}/purchase-orders", json=po_body(org, **overrides), headers=org.h()), 201
        )
    )


async def consume(
    org: COrg,
    po_id: str,
    amount: str,
    *,
    kind: str = "invoice",
    doc: uuid.UUID | None = None,
    currency: str = "INR",
    hours: str | None = None,
) -> Any:
    from app.services import commercials_service

    async with tenant_session(org.tenant_id) as db:
        return await commercials_service.record_po_consumption(
            org.ctx(),
            db,
            uuid.UUID(po_id),
            kind,
            doc or uuid.uuid4(),
            Decimal(amount),
            Decimal(hours) if hours else None,
            currency,
        )


__all__ = ["date", "gstin"]
