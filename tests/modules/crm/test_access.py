# ruff: noqa: E501
"""Epic 15: every CRM endpoint answers 404 for another tenant's records and refuses client / vendor portal
sessions (AD-26)."""

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from tests.modules.crm.conftest import API, CrmOrg, create_client, create_lead, create_opp, h, ok
from tests.p2 import External, make_client_user, make_vendor, make_vendor_user, sign_in_external

CRM_PREFIXES = ("/api/v1/crm", "/api/v1/leads", "/api/v1/lead-imports", "/api/v1/opportunities")
_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def _crm_routes(app: FastAPI) -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and (route.path.startswith(CRM_PREFIXES) or route.path.endswith("/opportunities")):
            out.extend((m, route.path) for m in sorted(route.methods - {"HEAD", "OPTIONS"}))
    return out


async def test_cross_tenant_404(corg: CrmOrg, other_corg: CrmOrg) -> None:
    client = await create_client(other_corg.admin, "Foreign Co")
    lead = await create_lead(other_corg.admin, "Foreign Lead")
    opp = await create_opp(other_corg.admin, client["id"], "Foreign deal", lines=[{"title": "Dev"}])
    c, hh = corg.admin.client, h(corg.admin)
    lid, oid = lead["id"], opp["id"]
    calls: list[tuple[str, str, Any]] = [
        ("GET", f"/leads/{lid}", None),
        ("PATCH", f"/leads/{lid}", {"industry": "x"}),
        ("POST", f"/leads/{lid}/transition", {"to": "qualified"}),
        ("POST", f"/leads/{lid}/archive", None),
        ("GET", f"/leads/{lid}/history", None),
        ("POST", f"/leads/{lid}/convert", {"client": {"mode": "link", "client_id": client["id"]}, "opportunity": {"name": "x"}}),
        ("GET", f"/lead-imports/{uuid.uuid4()}", None),
        ("GET", f"/opportunities/{oid}", None),
        ("PATCH", f"/opportunities/{oid}", {"name": "x"}),
        ("PUT", f"/opportunities/{oid}/lines", {"lines": []}),
        ("POST", f"/opportunities/{oid}/transition", {"to": "proposal", "expected_stage": "qualification"}),
        ("POST", f"/opportunities/{oid}/reset-probability", None),
        ("GET", f"/opportunities/{oid}/history", None),
        ("POST", f"/opportunities/{oid}/won-preview", None),
        ("GET", f"/clients/{client['id']}/opportunities", None),
        ("GET", f"/leads/{lid}/timeline", None),
        ("GET", f"/opportunities/{oid}/timeline", None),
        ("PATCH", f"/crm/reason-codes/{uuid.uuid4()}", {"label": "x"}),
        ("PATCH", f"/crm/lead-sources/{uuid.uuid4()}", {"label": "x"}),
    ]
    for method, path, body in calls:
        resp = await c.request(method, f"{API}{path}", json=body, headers=hh)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)
    win = await c.post(f"{API}/opportunities/{oid}/win", json={}, headers={**hh, "Idempotency-Key": "x"})
    assert win.status_code == 404
    # lists never leak
    assert (await ok(await c.get(f"{API}/leads")))["total"] == 0
    assert (await ok(await c.get(f"{API}/opportunities")))["total"] == 0
    link = await ok(await c.get(f"{API}/crm/links", params={"record_kind": "client", "record_id": client["id"]}))
    assert link is None


@pytest.fixture
async def externals(app: FastAPI, corg: CrmOrg) -> AsyncIterator[list[External]]:
    client = await create_client(corg.admin, "Portal Co")
    cid = uuid.UUID(client["id"])
    cu = await make_client_user(corg.tenant_id, cid)
    vendor = await make_vendor(corg.tenant_id)
    vu = await make_vendor_user(corg.tenant_id, vendor)
    out = [
        await sign_in_external(app, "client_user", cu, corg.tenant_id, cid),
        await sign_in_external(app, "vendor_user", vu, corg.tenant_id, vendor),
    ]
    yield out
    for e in out:
        await e.client.aclose()


async def test_portal_sessions_rejected(app: FastAPI, externals: list[External]) -> None:
    routes = _crm_routes(app)
    assert len(routes) >= 30
    offenders = []
    for external in externals:
        for method, path in routes:
            url = _PARAM.sub(lambda _m: str(uuid.uuid4()), path)
            resp = await external.client.request(method, url, headers=external.h(), json={} if method != "GET" else None)
            if resp.status_code != 401:
                offenders.append(f"{external.principal_type} {method} {path} -> {resp.status_code}")
    assert offenders == []
