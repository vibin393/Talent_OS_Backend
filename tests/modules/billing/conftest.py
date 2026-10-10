# ruff: noqa: E501
"""Fixtures for the Epic 29 billing tests: a tenant whose default Legal Entity is GST-registered in Karnataka (29)
with invoice / credit-note / debit-note series, a client with a registered KA billing profile, and a fake hour
posting source (``test_hours``) standing in for approved timesheets."""

import dataclasses
import uuid
from collections import defaultdict
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
from app.models.identity import Tenant
from app.schemas.legal_entities import (
    BankAccountIn,
    DocumentSeriesIn,
    GstRegistrationIn,
    LegalEntityUpdate,
    LutIn,
)
from app.services import legal_entities_service, postings
from tests.epic3 import _isolated_files, _no_real_ai, fake_ai  # noqa: F401
from tests.modules.demands.helpers import create_client
from tests.modules.identity.conftest import Member, csrf, join, make_tenant
from tests.modules.legal_entities.conftest import gstin, provision

API = "/api/v1"
B = f"{API}/billing"
PAN = "ABCDE1234F"
CLIENT_PAN = "AAACL1234C"
FAKE: dict[str, list[postings.SourceItem]] = defaultdict(list)
FAKE_KIND = "test_hours"


async def _fake_loader(
    ctx: RequestContext, db: Any, query: postings.SourceQuery
) -> list[postings.SourceItem]:
    tid = str(db.sync_session.info.get("tenant_id"))
    out = []
    for item in FAKE.get(tid, []):
        if item.item_date > query.period_end:
            continue
        if query.placement_ids is not None and item.placement_id not in set(query.placement_ids):
            continue
        out.append(item)
    return out


if FAKE_KIND not in postings.posting_sources():
    postings.register_posting_source(FAKE_KIND, _fake_loader, ledgers=frozenset({"billing"}))


@dataclasses.dataclass
class BOrg:
    tenant: Tenant
    admin: Member
    client_id: uuid.UUID
    entity_id: uuid.UUID
    registration_id: uuid.UUID

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
        principal = Principal(
            type="user",
            id=self.admin.user_id,
            tenant_id=self.tenant_id,
            permissions=dict.fromkeys(CATALOG, Scope.TENANT),
        )
        return RequestContext(tenant_id=self.tenant_id, principal=principal)


async def ok(resp: httpx.Response, code: int = 200) -> Any:
    assert resp.status_code == code, resp.text
    return resp.json() if resp.content else None


def err(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code, body
    return dict(body["error"])


async def billing_profile(
    org: BOrg,
    client_id: uuid.UUID,
    *,
    state: str | None = "29",
    treatment: str = "registered",
    country: str = "IN",
    sez: bool = False,
    with_gstin: bool = True,
) -> None:
    body = {
        "legal_name": f"Client {str(client_id)[:6]} Pvt Ltd",
        "pan": CLIENT_PAN,
        "tax_treatment": treatment,
    }
    await ok(await org.c.put(f"{API}/clients/{client_id}/billing-profile", json=body, headers=org.h()))
    address: dict[str, Any] = {
        "label": "HQ",
        "line1": "1 MG Road",
        "city": "City",
        "country": country,
        "is_sez": sez,
        "is_default": True,
    }
    if state:
        address["state_code"] = state
        address["postal_code"] = "560001"
    if with_gstin and state and country == "IN":
        address["gstin"] = gstin(state, CLIENT_PAN)
    await ok(
        await org.c.post(f"{API}/clients/{client_id}/billing-addresses", json=address, headers=org.h()), 201
    )


async def setup_entity(tenant_id: uuid.UUID, entity_id: uuid.UUID, ctx: RequestContext) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        await legal_entities_service.update_entity(
            ctx,
            db,
            entity_id,
            LegalEntityUpdate(
                pan=PAN,
                state_code="29",
                legal_name="Talent Staffing Pvt Ltd",
                signatory_name="Asha Rao",
                address={"line1": "2 Residency Rd", "city": "Bengaluru", "postal_code": "560025"},
            ),
        )
        reg = await legal_entities_service.create_registration(
            ctx,
            db,
            entity_id,
            GstRegistrationIn(
                gstin=gstin("29", PAN),
                state_code="29",
                is_principal=True,
                einvoice_applicable=True,
                irp_reporting_limit=True,
            ),
        )
        await legal_entities_service.create_bank_account(
            ctx,
            db,
            entity_id,
            BankAccountIn(
                label="Main",
                bank_name="HDFC Bank",
                account_number="50100012345678",
                ifsc="HDFC0000123",
                is_default=True,
            ),
        )
        for doc_type, prefix in (("invoice", "INV"), ("credit_note", "CN"), ("debit_note", "DN")):
            await legal_entities_service.create_series(
                ctx,
                db,
                entity_id,
                DocumentSeriesIn(gst_registration_id=reg.id, doc_type=doc_type, name=prefix, prefix=prefix),  # type: ignore[arg-type]
            )
        return reg.id


async def add_lut(org: BOrg, fy: str = "26-27") -> None:
    start = 2000 + int(fy[:2])
    async with tenant_session(org.tenant_id) as db:
        await legal_entities_service.add_lut(
            org.ctx(),
            db,
            org.registration_id,
            LutIn(
                fiscal_year=fy,
                lut_number=f"AD29{fy[:2]}00001",
                valid_from=date(start, 4, 1),
                valid_to=date(start + 1, 3, 31),
            ),
        )


async def _org(app: FastAPI, name: str) -> BOrg:
    tenant = await make_tenant(seat_limit=50)
    entity_id = await provision(tenant)
    admin = await join(app, tenant, name=name)
    client_id = await create_client(admin)
    org = BOrg(
        tenant=tenant, admin=admin, client_id=client_id, entity_id=entity_id, registration_id=uuid.uuid4()
    )
    org.registration_id = await setup_entity(tenant.id, entity_id, org.ctx())
    await billing_profile(org, client_id)
    return org


@pytest.fixture
async def borg(app: FastAPI) -> AsyncIterator[BOrg]:
    org = await _org(app, "Fiona Finance")
    yield org
    FAKE.pop(str(org.tenant_id), None)
    await org.admin.client.aclose()


@pytest.fixture
async def other_borg(app: FastAPI) -> AsyncIterator[BOrg]:
    org = await _org(app, "Oscar Other")
    yield org
    FAKE.pop(str(org.tenant_id), None)
    await org.admin.client.aclose()


async def placement(
    org: BOrg,
    client_id: uuid.UUID | None = None,
    *,
    currency: str = "INR",
    rate_unit: str = "hour",
    bill_rate: str = "1500.00",
    start: date = date(2026, 9, 1),
) -> uuid.UUID:
    from tests.modules.placements.helpers import direct_placement

    p = await direct_placement(
        org,  # type: ignore[arg-type]
        client_id=str(client_id or org.client_id),
        planned_start_date=start.isoformat(),
        currency=currency,
        rate_unit=rate_unit,
        bill_rate=bill_rate,
        pay_rate=str(Decimal(bill_rate) * Decimal("0.7")),
    )
    pid = uuid.UUID(p["id"])
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text(
                "UPDATE placements SET status = 'active', actual_start_date = :d, supplier_gst_registration_id = :r WHERE id = :i"
            ),
            {"d": start, "r": org.registration_id, "i": pid},
        )
    await ok(
        await org.c.put(
            f"{B}/placements/{pid}/rule",
            json={"basis": "monthly_fixed" if rate_unit == "month" else "hourly"},
            headers=org.h(),
        )
    )
    return pid


def hours(
    org: BOrg,
    placement_id: uuid.UUID,
    qty: str,
    on: date,
    *,
    category: str = "regular",
    currency: str = "INR",
    kind: str = FAKE_KIND,
    **attrs: Any,
) -> uuid.UUID:
    sid = uuid.uuid4()
    FAKE[str(org.tenant_id)].append(
        postings.SourceItem(
            kind,
            sid,
            placement_id,
            on,
            Decimal(qty),
            None,
            currency,
            {"category": category, "billable": True, "unit": "hour", **attrs},
        )
    )
    return sid


async def run(
    org: BOrg,
    start: date = date(2026, 9, 1),
    end: date = date(2026, 9, 30),
    key: str | None = None,
    **body: Any,
) -> dict[str, Any]:
    payload = {
        "legal_entity_id": str(org.entity_id),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        **body,
    }
    created = await ok(
        await org.c.post(f"{B}/runs", json=payload, headers=org.h(key or str(uuid.uuid4()))), 202
    )
    return dict(await ok(await org.c.get(f"{B}/runs/{created['id']}")))


async def issue(
    org: BOrg, invoice_id: str, *, doc_date: str | None = None, key: str | None = None, status: int = 200
) -> httpx.Response:
    body = {"doc_date": doc_date} if doc_date else {}
    resp = await org.c.post(
        f"{B}/invoices/{invoice_id}/issue", json=body, headers=org.h(key or str(uuid.uuid4()))
    )
    assert resp.status_code == status, resp.text
    return resp
