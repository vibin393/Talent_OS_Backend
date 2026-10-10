"""``/api/v1/semantic/*`` catalogue and validation endpoints (Epic 36 story 36.2)."""

import httpx
from fastapi import FastAPI

from tests.modules.identity.conftest import join, make_tenant
from tests.p2 import make_vendor, make_vendor_user, sign_in_external

BASE = "/api/v1/semantic"


async def test_catalogue_validate_and_masking(app: FastAPI) -> None:
    tenant = await make_tenant()
    admin = await join(app, tenant)
    recruiter = await join(app, tenant, "recruiter")

    resp = await admin.client.get(f"{BASE}/sources")
    assert resp.status_code == 200, resp.text
    keys = {s["key"] for s in resp.json()}
    assert {"candidates.candidate", "placements.worker"} <= keys

    resp = await recruiter.client.get(f"{BASE}/sources/candidates.candidate")
    assert resp.status_code == 200, resp.text
    fields = {f["key"]: f for f in resp.json()["fields"]}
    assert fields["expected_comp_inr"]["masked"] is True and fields["expected_comp_inr"]["ops"] == []
    assert fields["consent_marketing"]["ops"] == []
    assert fields["tags.name"]["displayable"] is False
    admin_fields = {
        f["key"]: f for f in (await admin.client.get(f"{BASE}/sources/candidates.candidate")).json()["fields"]
    }
    assert admin_fields["expected_comp_inr"]["masked"] is False
    assert "placements.status" in admin_fields

    resp = await recruiter.client.get(f"{BASE}/sources/nope.thing")
    assert resp.status_code == 404

    body = {"source": "candidates.candidate", "filters": {"field": "city", "op": "eq", "value": "Chennai"}}
    resp = await recruiter.client.post(f"{BASE}/validate", json=body, headers=recruiter.h())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"valid": True, "errors": []}

    body = {
        "source": "candidates.candidate",
        "filters": {"field": "expected_comp_inr", "op": "lt", "value": 1500000},
    }
    resp = await recruiter.client.post(f"{BASE}/validate", json=body, headers=recruiter.h())
    out = resp.json()
    assert out["valid"] is False and out["errors"][0]["code"] == "semantic_field_forbidden"
    assert out["errors"][0]["path"] == "filters.field"

    body = {"source": "candidates.candidate", "filters": {"field": "salary", "op": "eq", "value": 1}}
    resp = await admin.client.post(f"{BASE}/validate", json=body, headers=admin.h())
    assert resp.json()["errors"][0] == {
        "code": "semantic_unknown_field",
        "message": "Unknown field 'salary'",
        "path": "filters.field",
        "details": {},
    }

    body = {"source": "nope.thing"}
    resp = await recruiter.client.post(f"{BASE}/validate", json=body, headers=recruiter.h())
    assert resp.json()["errors"][0]["code"] == "semantic_unknown_source"


async def test_external_principal_refused(app: FastAPI, client: httpx.AsyncClient) -> None:
    tenant = await make_tenant()
    vendor_id = await make_vendor(tenant.id)
    vendor_user = await make_vendor_user(tenant.id, vendor_id)
    ext = await sign_in_external(app, "vendor_user", vendor_user, tenant.id, vendor_id)
    resp = await ext.client.get(f"{BASE}/sources")
    assert resp.status_code in {401, 403}
    resp = await client.get(f"{BASE}/sources")
    assert resp.status_code == 401
