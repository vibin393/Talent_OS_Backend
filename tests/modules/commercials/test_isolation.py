"""Isolation (AD-26): cross-tenant 404s, external sessions rejected, org RLS on the new tables, the client portal
PO projection and permissions (Epic 16 A10)."""  # noqa: E501

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import OrgScope, platform_session, tenant_session
from app.core.permissions import Principal
from app.services.commercials import portal
from tests.modules.commercials.helpers import (
    API,
    COrg,
    active_contract,
    consume,
    create_po,
    ok,
    published_card,
)
from tests.modules.identity.conftest import join
from tests.p2 import make_client_user, make_vendor, make_vendor_user, sign_in_external

ORG_TABLES = {
    "payment_terms",
    "billing_profiles",
    "billing_addresses",
    "billing_profile_contacts",
    "contract_documents",
    "contract_milestones",
    "contract_reminders",
    "rate_cards",
    "rate_card_lines",
    "rate_checks",
    "po_consumptions",
    "po_alerts",
    "commercials_settings",
}


async def _resources(org: COrg) -> dict[str, str]:
    contract = await active_contract(org)
    card = await published_card(org)
    po = await create_po(org)
    return {"contract": contract["id"], "card": card["id"], "po": po["id"], "client": str(org.client_id)}


def _gets(r: dict[str, str]) -> list[str]:
    return [
        f"{API}/contracts/{r['contract']}",
        f"{API}/contracts/{r['contract']}/documents",
        f"{API}/contracts/{r['contract']}/milestones",
        f"{API}/contracts/{r['contract']}/history",
        f"{API}/clients/{r['client']}/contracts",
        f"{API}/clients/{r['client']}/billing-profile",
        f"{API}/clients/{r['client']}/rate-cards",
        f"{API}/clients/{r['client']}/purchase-orders",
        f"{API}/rate-cards/{r['card']}",
        f"{API}/purchase-orders/{r['po']}",
        f"{API}/purchase-orders/{r['po']}/consumptions",
        f"{API}/purchase-orders/{r['po']}/placements",
    ]


def _posts(r: dict[str, str]) -> list[str]:
    return [
        f"{API}/contracts/{r['contract']}/activate",
        f"{API}/contracts/{r['contract']}/submit-for-approval",
        f"{API}/rate-cards/{r['card']}/publish",
        f"{API}/rate-cards/{r['card']}/new-version",
        f"{API}/purchase-orders/{r['po']}/close",
        f"{API}/purchase-orders/{r['po']}/cancel",
    ]


async def test_cross_tenant_404(corg: COrg, other_corg: COrg) -> None:
    r = await _resources(corg)
    for url in _gets(r):
        resp = await other_corg.c.get(url)
        assert resp.status_code == 404, (url, resp.text)
    for url in _posts(r):
        resp = await other_corg.c.post(url, headers=other_corg.h())
        assert resp.status_code == 404, (url, resp.text)
    # The owner still sees them.
    for url in _gets(r):
        assert (await corg.c.get(url)).status_code == 200, url
    # Lists of another tenant do not leak rows.
    assert await ok(await other_corg.c.get(f"{API}/contracts")) == []
    assert await ok(await other_corg.c.get(f"{API}/purchase-orders")) == []


async def test_external_sessions_rejected(app: FastAPI, corg: COrg) -> None:
    r = await _resources(corg)
    cu = await make_client_user(corg.tenant_id, corg.client_id)
    client_session = await sign_in_external(app, "client_user", cu, corg.tenant_id, corg.client_id)
    vendor_id = await make_vendor(corg.tenant_id)
    vu = await make_vendor_user(corg.tenant_id, vendor_id)
    vendor_session = await sign_in_external(app, "vendor_user", vu, corg.tenant_id, vendor_id)
    try:
        for ext in (client_session, vendor_session):
            for url in [
                *_gets(r),
                f"{API}/contracts",
                f"{API}/purchase-orders",
                f"{API}/payment-terms",
                f"{API}/commercials/settings",
            ]:
                resp = await ext.client.get(url)
                assert resp.status_code in (401, 403), (url, resp.status_code)
            for url in _posts(r):
                resp = await ext.client.post(url, headers=ext.h())
                assert resp.status_code in (401, 403), (url, resp.status_code)
    finally:
        await client_session.client.aclose()
        await vendor_session.client.aclose()


async def test_permissions_required(app: FastAPI, corg: COrg) -> None:
    r = await _resources(corg)
    viewer = await join(app, corg.tenant, "viewer", name="Vic Viewer")
    try:
        for url in (
            f"{API}/contracts/{r['contract']}",
            f"{API}/purchase-orders/{r['po']}",
            f"{API}/rate-cards/{r['card']}",
        ):
            assert (await viewer.client.get(url)).status_code == 403, url
        assert (await viewer.client.get(f"{API}/payment-terms")).status_code == 200
    finally:
        await viewer.client.aclose()


async def test_org_rls_on_every_new_table() -> None:
    async with platform_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT c.relname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                    "WHERE p.polname = 'org_isolation' AND c.relname = ANY(:t)"
                ),
                {"t": sorted(ORG_TABLES)},
            )
        ).all()
    assert {r[0] for r in rows} == ORG_TABLES


async def test_client_portal_po_projection(corg: COrg, other_corg: COrg) -> None:
    po = await create_po(corg, po_number="PO-CL-1")
    await consume(corg, po["id"], "1000")
    await create_po(other_corg, po_number="PO-OTHER")
    principal = Principal(
        type="client_user", id=uuid.uuid4(), tenant_id=corg.tenant_id, org_id=corg.client_id
    )
    ctx = RequestContext(tenant_id=corg.tenant_id, principal=principal)
    async with tenant_session(corg.tenant_id, org=OrgScope.of("client", corg.client_id)) as db:
        rows = await portal.client_po_consumption(ctx, db)
    assert [(r.po_number, r.consumed_amount) for r in rows] == [("PO-CL-1", 1000)]
    # Another client's scope sees nothing of this client's POs (org RLS).
    from tests.modules.demands.helpers import create_client

    stranger = await create_client(corg.admin)
    ctx2 = RequestContext(
        tenant_id=corg.tenant_id,
        principal=Principal(type="client_user", id=uuid.uuid4(), tenant_id=corg.tenant_id, org_id=stranger),
    )
    async with tenant_session(corg.tenant_id, org=OrgScope.of("client", stranger)) as db:
        assert await portal.client_po_consumption(ctx2, db) == []
    with pytest.raises(Exception):  # noqa: B017
        async with tenant_session(corg.tenant_id) as db:
            await portal.client_po_consumption(corg.ctx(), db)
