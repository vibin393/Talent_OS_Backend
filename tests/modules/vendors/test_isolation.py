"""Epic 17 A12 isolation (NFR-22, AD-26): every ``/vendor-portal/*`` route with another vendor's (same
tenant) or
another tenant's ids is a 404 -- never 403 / 200; vendor cookies never reach internal routes; tenant-user
cookies never reach the portal; vendor tables fail closed without an org GUC; FR-80 field visibility."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import app_engine, tenant_session
from app.core.field_visibility import registered_schemas, violations
from app.models.vendors import VendorSubmission
from tests.epic3 import make_pdf
from tests.modules.demands.helpers import DOrg, open_demand
from tests.modules.vendors.conftest import PORTAL, Setup, active_vendor, release, submit, vendor_user
from tests.p2 import External

TABLES = (
    "vendor_releases",
    "vendor_submissions",
    "vendor_submission_events",
    "vendor_gstins",
    "vendor_bank_accounts",
    "vendor_document_uploads",
    "vendor_payable_statements",
    "vendor_statement_lines",
    "vendor_candidate_ownerships",
    "vendor_settings",
)


async def _foreign_ids(app: FastAPI, org: DOrg, demand_id: str) -> dict[str, str]:
    """Vendor B in ``org``: a release, a submission (with resume file) and a vendor user."""
    vendor_b = await active_vendor(org, "Vendor B")
    rel = (await release(org, demand_id, [vendor_b["id"]]))["items"][0]["release"]
    ext_b = await vendor_user(app, org, vendor_b["id"])
    try:
        sub = (await submit(ext_b, rel["id"])).json()
    finally:
        await ext_b.client.aclose()
    async with tenant_session(org.tenant_id) as db:
        file_id = (
            await db.execute(
                select(VendorSubmission.resume_file_id).where(VendorSubmission.id == uuid.UUID(sub["id"]))
            )
        ).scalar_one()
    return {
        "release": rel["id"],
        "submission": sub["id"],
        "file": str(file_id),
        "user": str(ext_b.id),
        "statement": str(uuid.uuid4()),
    }


def _calls(ids: dict[str, str], ext: External) -> list[tuple[str, str, dict[str, Any]]]:
    pdf = {"files": {"resume": ("cv.pdf", make_pdf("x"), "application/pdf")}}
    form = {
        "data": {"full_name": "X Y", "email": "x@y.example.com", "vendor_rate": "100", "attestation": "true"},
        "files": {"resume": ("cv.pdf", make_pdf("x"), "application/pdf")},
    }
    return [
        ("GET", f"{PORTAL}/releases/{ids['release']}", {}),
        ("POST", f"{PORTAL}/releases/{ids['release']}/submissions", form),
        ("GET", f"{PORTAL}/submissions/{ids['submission']}", {}),
        ("POST", f"{PORTAL}/submissions/{ids['submission']}/withdraw", {}),
        ("POST", f"{PORTAL}/submissions/{ids['submission']}/resume", pdf),
        ("GET", f"{PORTAL}/statements/{ids['statement']}/download", {}),
        ("GET", f"{PORTAL}/files/{ids['file']}/download", {}),
    ]


async def test_cross_vendor_and_cross_tenant_404(app: FastAPI, setup: Setup, other_dorg: DOrg) -> None:
    same_tenant = await _foreign_ids(app, setup.org, setup.demand["id"])
    demand2 = await open_demand(other_dorg.admin, other_dorg.client_id)
    other_tenant = await _foreign_ids(app, other_dorg, demand2["id"])
    for ids in (same_tenant, other_tenant):
        for method, url, kw in _calls(ids, setup.user):
            resp = await setup.user.client.request(method, url, headers=setup.user.h(), **kw)
            assert resp.status_code == 404, (method, url, resp.status_code, resp.text)
    # Lists never include the other vendor's rows.
    listing = (await setup.user.client.get(f"{PORTAL}/submissions")).text
    assert same_tenant["submission"] not in listing and other_tenant["submission"] not in listing
    releases = (await setup.user.client.get(f"{PORTAL}/releases")).text
    assert same_tenant["release"] not in releases


async def test_vendor_cookie_rejected_on_internal_routes_and_user_cookie_on_portal(setup: Setup) -> None:
    for url in (
        "/api/v1/candidates",
        "/api/v1/vendors",
        f"/api/v1/vendor-submissions/{uuid.uuid4()}",
        "/api/v1/vendor-statements",
    ):
        resp = await setup.user.client.get(url)
        assert resp.status_code == 401, (url, resp.status_code)
    for url in (f"{PORTAL}/dashboard", f"{PORTAL}/releases", f"{PORTAL}/submissions"):
        resp = await setup.org.admin.client.get(url)
        assert resp.status_code == 401, (url, resp.status_code)


async def test_vendor_tables_fail_closed_without_org_guc(setup: Setup) -> None:
    async with app_engine().connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(setup.org.tenant_id)}
        )
        for table in TABLES:
            assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0, table
        await conn.rollback()


async def test_internal_only_tables_invisible_to_vendor_session(setup: Setup) -> None:
    from app.core.database import OrgScope

    async with tenant_session(
        setup.org.tenant_id, org=OrgScope.of("vendor", uuid.UUID(setup.vendor["id"]))
    ) as db:
        for table in ("vendor_candidate_ownerships", "vendor_settings", "vendor_document_requirements"):
            assert (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0, table
        assert (await db.execute(text("SELECT count(*) FROM vendor_releases"))).scalar_one() == 1


def test_vendor_portal_schemas_have_no_forbidden_fields() -> None:
    import app.schemas.vendors_portal as mod

    models = [m for m, kinds in registered_schemas().items() if m.__module__ == mod.__name__]
    assert len(models) >= 10
    for model in models:
        assert violations(model, "vendor_user") == [], model.__name__
        assert "candidate_id" not in model.model_fields, model.__name__


@pytest.mark.parametrize("reveal", [False, True])
async def test_client_name_only_when_revealed(app: FastAPI, dorg: DOrg, reveal: bool) -> None:
    v = await active_vendor(dorg)
    demand = await open_demand(dorg.admin, dorg.client_id)
    await release(dorg, demand["id"], [v["id"]], reveal_client=reveal)
    ext = await vendor_user(app, dorg, v["id"])
    try:
        [rel] = (await ext.client.get(f"{PORTAL}/releases")).json()
    finally:
        await ext.client.aclose()
    if reveal:
        assert rel["client_name"] != "Confidential client"
    else:
        assert rel["client_name"] == "Confidential client"
    for forbidden in ("bill_rate", "margin", "client_feedback", "pay_rate_min"):
        assert forbidden not in rel


async def test_internal_endpoints_cross_tenant_404(setup: Setup, other_dorg: DOrg) -> None:
    other = other_dorg.admin.client
    for url in (
        f"/api/v1/vendors/{setup.vendor['id']}",
        f"/api/v1/vendors/{setup.vendor['id']}/documents",
        f"/api/v1/vendors/{setup.vendor['id']}/performance",
        f"/api/v1/vendor-submissions/{uuid.uuid4()}",
        f"/api/v1/demands/{setup.demand['id']}/vendor-releases",
    ):
        resp = await other.get(url)
        assert resp.status_code == 404, (url, resp.status_code)
