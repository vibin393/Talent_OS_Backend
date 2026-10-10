# ruff: noqa: E501
"""Fixtures for the Epic 30 receivables tests: a tenant with its default Legal Entity (open accounting periods
Dec 2025 - Mar 2027), a client and a signed-in Tenant Admin. Receivable documents are inserted directly as
issued rows (billing tables are read, never written, by the code under test)."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.models.billing import CreditNote, DebitNote, Invoice
from app.models.identity import Tenant
from app.services import legal_entities_service
from tests.epic3 import _isolated_files, _no_real_ai, fake_ai  # noqa: F401
from tests.modules.demands.helpers import create_client
from tests.modules.identity.conftest import Member, csrf, join, make_tenant
from tests.modules.legal_entities.conftest import provision

API = "/api/v1"
R = f"{API}/receivables"


@dataclasses.dataclass
class ROrg:
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

    def h(self, key: str | None = None) -> dict[str, str]:
        headers = csrf(self.admin.client)
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def ctx(self) -> RequestContext:
        principal = Principal(type="user", id=self.admin.user_id, tenant_id=self.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT))
        return RequestContext(tenant_id=self.tenant_id, principal=principal)

    def session(self) -> Any:
        return tenant_session(self.tenant_id)


async def _org(app: FastAPI, name: str) -> ROrg:
    tenant = await make_tenant(seat_limit=50)
    entity_id = await provision(tenant)
    admin = await join(app, tenant, name=name)
    client_id = await create_client(admin)
    async with tenant_session(tenant.id) as db:
        await legal_entities_service.ensure_periods(db, entity_id, date(2025, 12, 1), date(2027, 3, 1))
    return ROrg(tenant=tenant, admin=admin, client_id=client_id, entity_id=entity_id)


@pytest.fixture
async def rorg(app: FastAPI) -> AsyncIterator[ROrg]:
    org = await _org(app, "Rita Receivable")
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_rorg(app: FastAPI) -> AsyncIterator[ROrg]:
    org = await _org(app, "Omar Other")
    yield org
    await org.admin.client.aclose()


_n = 0


def _number(prefix: str) -> str:
    global _n  # test-local counter
    _n += 1
    return f"{prefix}{uuid.uuid4().hex[:6].upper()}{_n}"[:16]


async def invoice(
    org: ROrg,
    total: str,
    *,
    taxable: str | None = None,
    currency: str = "INR",
    fx: str | None = None,
    inr_total: str | None = None,
    doc_date: date = date(2026, 9, 1),
    due: date = date(2026, 9, 30),
    client_id: uuid.UUID | None = None,
    kind: str = "invoice",
    original_invoice_id: uuid.UUID | None = None,
) -> uuid.UUID:
    amount = Decimal(total)
    inr = Decimal(inr_total) if inr_total else (amount if currency == "INR" else (amount * Decimal(fx or 1)).quantize(Decimal("0.01")))
    model: Any = {"invoice": Invoice, "debit_note": DebitNote, "credit_note": CreditNote}[kind]
    values: dict[str, Any] = {
        "id": uuid.uuid4(), "tenant_id": org.tenant_id, "legal_entity_id": org.entity_id, "client_id": client_id or org.client_id,
        "number": _number({"invoice": "INV", "debit_note": "DN", "credit_note": "CN"}[kind]), "doc_date": doc_date, "status": "issued",
        "currency": currency, "document_total": amount, "taxable_total": Decimal(taxable or total), "inr_document_total": inr,
        "fx_rate": Decimal(fx) if fx else None, "fiscal_year": "26-27",
    }  # fmt: skip
    if kind != "credit_note":
        values["due_date"] = due
    if kind != "invoice":
        values["original_invoice_id"] = original_invoice_id
    if kind == "credit_note":
        values["reason"] = "commercial"
    async with tenant_session(org.tenant_id) as db:
        db.add(model(**values))
    return values["id"]


async def ok(resp: httpx.Response, code: int = 200) -> Any:
    assert resp.status_code == code, resp.text
    return resp.json() if resp.content else None


def err(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code, body
    return dict(body["error"])


async def close_month(org: ROrg, year: int, month: int) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(text("UPDATE accounting_periods SET status = 'closed' WHERE legal_entity_id = :le AND year = :y AND month = :m"), {"le": org.entity_id, "y": year, "m": month})
