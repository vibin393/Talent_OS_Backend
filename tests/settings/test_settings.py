"""Story 12.8 (FR-74): settings hub catalog, company / regional / ID prefixes / link expiries with field-level
422s and audit. Matrix rows 33, 34, 35."""

import asyncio
import uuid

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.settings import TenantSettings
from app.services import settings_service
from tests.epic12 import Org3, create_candidate, h, member
from tests.factories import create_tenant_row

SETTINGS = "/api/v1/settings"


async def test_catalog_lists_sections_and_unbuilt_domains(org3: Org3) -> None:
    catalog = (await org3.admin.client.get(SETTINGS)).json()
    keys = {e["key"]: e for e in catalog}
    assert {"company", "regional", "id_prefixes", "link_expiries", "ai_features", "retention", "consent"} <= set(keys)
    assert {"organization", "services", "security_privacy"} <= {e["category"] for e in catalog}  # + Epic 35 registry
    assert all(isinstance(e["available"], bool) for e in catalog)
    sections = (await org3.admin.client.get(f"{SETTINGS}/sections")).json()
    assert sections["regional"]["timezone"] == "Asia/Kolkata"
    assert sections["link_expiries"]["consent_request"] == 7
    assert sections["id_prefixes"]["CAN"] == "CAN"


async def test_regional_validation_and_tenant_write(org3: Org3) -> None:
    """Matrix 33: 'Mars/Base' -> 422 on details.fields.timezone."""
    bad = await org3.admin.client.patch(
        f"{SETTINGS}/regional", json={"timezone": "Mars/Base", "currency": "XYZ"}, headers=h(org3.admin)
    )
    assert bad.status_code == 422
    fields = bad.json()["error"]["details"]["fields"]
    assert set(fields) == {"timezone", "currency"}
    ok = await org3.admin.client.patch(
        f"{SETTINGS}/regional",
        json={"timezone": "Asia/Dubai", "currency": "AED", "date_format": "YYYY-MM-DD", "number_format": "international"},
        headers=h(org3.admin),
    )
    assert ok.status_code == 200, ok.text
    assert (ok.json()["timezone"], ok.json()["currency"], ok.json()["number_format"]) == ("Asia/Dubai", "AED", "international")
    audit = (await org3.admin.client.get("/api/v1/admin/audit-logs", params={"action": "tenant.regional_updated"})).json()
    assert audit["total"] == 1
    changed = (await org3.admin.client.get("/api/v1/admin/audit-logs", params={"action": "settings.updated"})).json()
    assert changed["items"][0]["data"]["section"] == "regional"


async def test_id_prefix_change_continues_numbering(org3: Org3) -> None:
    """Matrix 34: CAN -> CND, the next ID continues the same sequence."""
    first = await create_candidate(org3.admin)
    number = int(first["human_id"].split("-")[1])
    dup = await org3.admin.client.patch(f"{SETTINGS}/id-prefixes", json={"CAN": "DSR"}, headers=h(org3.admin))
    assert dup.status_code == 422 and dup.json()["error"]["details"]["fields"]  # CAN and DSR would collide
    bad = await org3.admin.client.patch(f"{SETTINGS}/id-prefixes", json={"CAN": "cnd1"}, headers=h(org3.admin))
    assert bad.status_code == 422
    ok = await org3.admin.client.patch(f"{SETTINGS}/id-prefixes", json={"CAN": "CND"}, headers=h(org3.admin))
    assert ok.status_code == 200, ok.text
    second = await create_candidate(org3.admin)
    assert second["human_id"] == f"CND-{number + 1:06d}"


async def test_link_expiries_and_link_ttl(org3: Org3) -> None:
    bad = await org3.admin.client.patch(
        f"{SETTINGS}/link-expiries", json={"dsr_export": 30, "consent_request": 0}, headers=h(org3.admin)
    )
    assert set(bad.json()["error"]["details"]["fields"]) == {"dsr_export", "consent_request"}
    ok = await org3.admin.client.patch(f"{SETTINGS}/link-expiries", json={"consent_request": 3}, headers=h(org3.admin))
    assert ok.json()["consent_request"] == 3
    async with tenant_session(org3.tenant_id) as db:
        assert (await settings_service.link_ttl(db, "consent_request")).days == 3
        assert (await settings_service.link_ttl(db, "dsr_export")).days == 7


async def test_company_and_permissions(app: FastAPI, org3: Org3, other3: Org3) -> None:
    resp = await org3.admin.client.patch(
        f"{SETTINGS}/company",
        json={"display_name": "Initech Talent", "grievance_officer": {"name": "Gita", "email": "gita@initech.example"}},
        headers=h(org3.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["grievance_officer"]["name"] == "Gita"
    assert (await other3.admin.client.get(f"{SETTINGS}/sections")).json()["company"] == {}
    missing_logo = await org3.admin.client.patch(
        f"{SETTINGS}/company", json={"logo_file_id": str(uuid.uuid4())}, headers=h(org3.admin)
    )
    assert missing_logo.status_code == 422
    recruiter = await member(app, org3, "recruiter")
    try:
        assert (await recruiter.client.get(SETTINGS)).status_code == 403
    finally:
        await recruiter.client.aclose()


async def test_catalog_endpoints_are_real_routes(app: FastAPI, org3: Org3) -> None:
    """Every available catalog entry points at a mounted route (e.g. matching weights ->
    ``/api/v1/settings/matching``). Organization sections are PATCH-only (read via ``/settings/sections``)."""
    catalog = (await org3.admin.client.get(SETTINGS)).json()
    paths = {getattr(r, "path", None) for r in app.routes}
    keys = {e["key"]: e for e in catalog}
    if "matching_weights" in keys:
        assert keys["matching_weights"]["endpoint"] == "/api/v1/settings/matching"
    missing = [e["endpoint"] for e in catalog if e["available"] and e["endpoint"] not in paths]
    assert missing == []


async def test_sections_first_access_is_race_safe() -> None:
    """Cold-start race (FR-74): the first requests for a tenant lazily create ``tenant_settings``. Several
    concurrent first requests all see no row and all try to insert the same primary key; one wins and the
    others must recover by re-reading rather than surfacing a duplicate-key IntegrityError as an HTTP 500.

    A barrier releases every session to call ``get_row`` at once so their inserts genuinely contend (this
    fails with a raw ``IntegrityError`` without the savepoint guard). Asserts every concurrent ``get_row``
    returns the row and exactly one row ends up persisted."""
    tenant = await create_tenant_row()
    release = asyncio.Event()

    async def one() -> uuid.UUID:
        async with tenant_session(tenant) as db:
            await release.wait()  # poise every session, then create concurrently
            return (await settings_service.get_row(db, tenant)).tenant_id

    tasks = [asyncio.create_task(one()) for _ in range(8)]
    await asyncio.sleep(0.1)  # let all sessions open and reach the barrier
    release.set()
    results = await asyncio.gather(*tasks)
    assert all(r == tenant for r in results)
    async with tenant_session(tenant) as db:
        rows = (await db.execute(select(TenantSettings))).scalars().all()
        assert len(rows) == 1 and rows[0].tenant_id == tenant
