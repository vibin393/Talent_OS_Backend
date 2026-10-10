# ruff: noqa: E501
"""Epic 69 (story 69.1, spec A3 / A8): manifests -> listings sync (idempotent), legacy listings, private Apps only in
their Tenant, policy-disallowed listings stay visible, the SDK content and the HTTP permission split."""

import uuid

from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.database import platform_session, tenant_session
from app.models.connectors import ProviderConnectorConfig
from app.models.marketplace import MarketplaceListing, MarketplaceListingVersion
from app.schemas.ecosystem import AppPolicyIn
from app.schemas.marketplace import ListingQuery
from app.services import ecosystem_service, marketplace_service
from app.services.marketplace import platform_catalog
from tests.epic3 import Org3, h
from tests.marketplace.helpers import KEY, human, install_connector
from tests.support.oauth import make_app


async def test_platform_catalog_sync_is_idempotent() -> None:
    await platform_catalog.sync()
    async with platform_session() as pdb:
        listing = (
            await pdb.execute(select(MarketplaceListing).where(MarketplaceListing.connector_key == KEY))
        ).scalar_one()
        n = (
            await pdb.execute(select(func.count()).where(MarketplaceListingVersion.listing_id == listing.id))
        ).scalar_one()
    assert listing.kind == "connector" and listing.category == "job_boards" and listing.status == "published"
    assert await platform_catalog.sync() == []
    async with platform_session() as pdb:
        assert (
            await pdb.execute(select(func.count()).where(MarketplaceListingVersion.listing_id == listing.id))
        ).scalar_one() == n


async def test_catalogue_cards_states_private_and_policy(org3: Org3, other3: Org3) -> None:
    private = await make_app(
        name="Acme Private", publisher_kind="tenant", owner_tenant_id=org3.tenant_id, status="development"
    )
    inst = await install_connector(org3)
    async with tenant_session(org3.tenant_id) as db:
        db.add(ProviderConnectorConfig(id=uuid.uuid4(), tenant_id=org3.tenant_id, category="sms", provider_key="msg91",
                                       display_name="SMS", settings={}, status="active"))  # fmt: skip
    async with tenant_session(org3.tenant_id) as db:
        page = await marketplace_service.list_listings(human(org3), db, ListingQuery(page_size=200))
    cards = {c.slug: c for c in page.data}
    board = cards["fake-job-board"]
    assert (
        board.install_state == "installed"
        and board.installation_id == inst.id
        and board.publisher_badge == "Talent OS"
    )
    assert (
        cards["legacy-sms"].install_state == "configured"
        and cards["legacy-sms"].manage_path == "/admin/messaging?tab=connectors"
    )
    assert cards[f"private-{private.slug}"].publisher_badge == "Private"
    async with tenant_session(other3.tenant_id) as db:
        other = await marketplace_service.list_listings(human(other3), db, ListingQuery(page_size=200))
    assert f"private-{private.slug}" not in {c.slug for c in other.data}
    # filters + policy
    async with tenant_session(org3.tenant_id) as db:
        jobs = await marketplace_service.list_listings(
            human(org3), db, ListingQuery(category="job_boards", q="fake")
        )
        assert [c.slug for c in jobs.data] == ["fake-job-board"]
        await ecosystem_service.set_app_policy(human(org3), db, AppPolicyIn(publisher_kinds=["partner"]))
    async with tenant_session(org3.tenant_id) as db:
        card = (await marketplace_service.list_listings(human(org3), db, ListingQuery(q="fake job"))).data[0]
        assert not card.install_allowed and card.not_allowed_reason == "Not allowed by your app policy"
        detail = await marketplace_service.get_listing(human(org3), db, "fake-job-board")
        assert detail.config_schema is not None and [s["name"] for s in detail.secret_fields] == [
            "api_key",
            "webhook_secret",
        ]
        assert detail.data_flows and detail.manifest_sha256


def test_sdk_content() -> None:
    schema = marketplace_service.sdk_manifest_schema()
    assert schema["connector_manifest"]["title"] == "ConnectorManifest"
    assert "job_board.publish" in marketplace_service.sdk_ports()["capabilities"]


async def test_http_permissions_and_cross_tenant(app: FastAPI, org3: Org3, other3: Org3) -> None:
    from tests.epic3 import member

    viewer = await member(app, org3, "recruiter", name="Vera Viewer")
    resp = await viewer.client.get("/api/v1/marketplace/listings", headers=h(viewer))
    assert resp.status_code == 403  # no integration:view
    resp = await org3.admin.client.get(
        "/api/v1/marketplace/listings?category=job_boards", headers=h(org3.admin)
    )
    assert resp.status_code == 200 and any(c["slug"] == "fake-job-board" for c in resp.json()["data"])
    inst = await install_connector(org3)
    resp = await other3.admin.client.get(
        f"/api/v1/marketplace/installations/{inst.id}", headers=h(other3.admin)
    )
    assert resp.status_code == 404
    resp = await org3.admin.client.get(f"/api/v1/marketplace/installations/{inst.id}", headers=h(org3.admin))
    assert resp.status_code == 200 and "k-123456" not in resp.text
    body = {"kind": "connector", "connector_key": KEY, "accept_manifest_sha256": "0" * 64}
    resp = await org3.admin.client.post("/api/v1/marketplace/installations", json=body, headers=h(org3.admin))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "idempotency_key_required"
