# ruff: file-ignore[line-too-long, invalid-formatter-suppression-comment]
"""Marketplace catalogue read model (Epic 69, spec A3, FR-354).

One list of: platform connectors (listing rows, falling back to the in-code manifests when the rows are missing),
legacy P2-P4 connectors (``legacy_catalog``; "Open settings" -> their existing screen), calendar connections, published
partner Apps and the caller Tenant's private Apps (built on read; never stored as listings, never shown to another
Tenant). Each card carries the Tenant's install state. Listings disallowed by the app policy stay visible with
"Not allowed by your app policy" (the Install action is refused).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import NotFound
from app.core.integrations import ConnectorManifest, find_manifest, manifest_sha256, manifests
from app.core.oauth_scopes import is_sensitive, scopes
from app.models.connectors import ProviderConnectorConfig
from app.models.marketplace import MarketplaceListing, MarketplaceListingVersion
from app.models.oauth import App, AppInstallation, PartnerOrg
from app.schemas.marketplace import (
    DataFlowLineOut,
    ListingCardOut,
    ListingOut,
    ListingPageOut,
    ListingQuery,
    ListingVersionLineOut,
    ScopeLineOut,
)
from app.services.marketplace import platform_catalog
from app.services.marketplace import policy as app_policy
from app.services.marketplace.legacy_catalog import (
    CATEGORY_LABELS,
    LEGACY_LISTINGS,
    by_slug,
    listing_category,
)

PRIVATE_PREFIX = "private-"
STATE_BY_STATUS = {
    "active": "installed",
    "pending_consent": "pending_consent",
    "pending_approval": "pending_approval",
    "reconsent_required": "reconsent_required",
    "degraded": "degraded",
    "suspended": "suspended",
}


def _badge(publisher_kind: str, partner_verified: bool) -> str:
    if publisher_kind == "platform":
        return "Talent OS"
    if publisher_kind == "tenant":
        return "Private"
    return "Verified partner" if partner_verified else "Partner"


class _Ctx:
    """Per-request lookups shared by list / get."""

    def __init__(
        self, policy: dict[str, Any], installs: list[AppInstallation], legacy_configured: set[str]
    ) -> None:
        self.policy = policy
        self.by_connector: dict[str, AppInstallation] = {}
        self.by_app: dict[uuid.UUID, AppInstallation] = {}
        for inst in installs:
            if inst.kind == "connector" and inst.connector_key:
                self.by_connector.setdefault(inst.connector_key, inst)
            elif inst.app_id is not None:
                self.by_app[inst.app_id] = inst
        self.legacy_configured = legacy_configured


async def _lookups(ctx: RequestContext, db: AsyncSession) -> _Ctx:
    assert ctx.tenant_id is not None
    policy = await app_policy.current(db, ctx.tenant_id)
    installs = list(
        (await db.execute(select(AppInstallation).where(AppInstallation.status != "uninstalled"))).scalars()
    )
    legacy = set(
        (
            await db.execute(
                select(ProviderConnectorConfig.category).where(
                    ProviderConnectorConfig.status == "active",
                    ProviderConnectorConfig.installation_id.is_(None),
                )
            )
        ).scalars()
    )
    return _Ctx(policy, installs, legacy)


def _state(inst: AppInstallation | None) -> tuple[str, uuid.UUID | None]:
    if inst is None:
        return "not_installed", None
    return STATE_BY_STATUS.get(inst.status, "installed"), inst.id


def _allowed(lk: _Ctx, kind: str, publisher_kind: str) -> tuple[bool, str | None]:
    if kind in ("legacy_connector", "calendar"):
        return False, None
    if app_policy.allows(lk.policy, publisher_kind):
        return True, None
    return False, app_policy.PUBLISHER_NOT_ALLOWED


def _connector_card(lk: _Ctx, m: ConnectorManifest, row: MarketplaceListing | None) -> ListingCardOut:
    kind = "calendar" if m.category == "calendar" else "connector"
    state, inst_id = _state(lk.by_connector.get(m.key))
    allowed, why = _allowed(lk, kind, "platform")
    return ListingCardOut(
        slug=str(row.slug) if row is not None else platform_catalog.slug_for(m.key), kind=kind,  # type: ignore[arg-type]
        name=row.name if row is not None else m.name, tagline=row.tagline if row is not None else (m.summary or m.name)[:200],
        category=listing_category(m.category), publisher_kind="platform", publisher_badge="Talent OS",
        install_state=state, install_allowed=allowed, not_allowed_reason=why, installation_id=inst_id,  # type: ignore[arg-type]
        manage_path=platform_catalog.CALENDAR_MANAGE_PATH if kind == "calendar" else None,
        featured=bool(row.featured) if row is not None else False,
    )  # fmt: skip


def _app_card(lk: _Ctx, app: App, row: MarketplaceListing | None, partner_verified: bool) -> ListingCardOut:
    publisher = app.publisher_kind
    state, inst_id = _state(lk.by_app.get(app.id))
    allowed, why = _allowed(lk, "app", publisher)
    slug = str(row.slug) if row is not None else f"{PRIVATE_PREFIX}{app.slug}"
    return ListingCardOut(
        slug=slug, kind="app", name=row.name if row is not None else app.name,
        tagline=row.tagline if row is not None else str((app.manifest or {}).get("description") or app.name)[:200],
        category=row.category if row is not None else str((app.manifest or {}).get("category") or "other"),
        publisher_kind=publisher, publisher_badge=_badge(publisher, partner_verified),  # type: ignore[arg-type]
        install_state=state, install_allowed=allowed and app.status in ("published", "development"), not_allowed_reason=why,  # type: ignore[arg-type]
        installation_id=inst_id, featured=bool(row.featured) if row is not None else False,
    )  # fmt: skip


def _legacy_cards(lk: _Ctx) -> list[ListingCardOut]:
    return [
        ListingCardOut(
            slug=x.slug,
            kind="legacy_connector",
            name=x.name,
            tagline=x.tagline,
            category=x.category,
            publisher_kind="platform",
            publisher_badge="Talent OS",
            install_state="configured" if x.key in lk.legacy_configured else "not_installed",
            install_allowed=False,
            manage_path=x.manage_path,
        )  # fmt: skip
        for x in LEGACY_LISTINGS
    ]


async def _listing_rows() -> tuple[list[MarketplaceListing], dict[uuid.UUID, App], dict[uuid.UUID, bool]]:
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(MarketplaceListing).where(
                        MarketplaceListing.status == "published",
                        MarketplaceListing.publisher_kind != "tenant",
                    )
                )
            ).scalars()
        )
        app_ids = [r.app_id for r in rows if r.app_id is not None]
        apps = (
            {a.id: a for a in (await pdb.execute(select(App).where(App.id.in_(app_ids)))).scalars()}
            if app_ids
            else {}
        )
        org_ids = [r.partner_org_id for r in rows if r.partner_org_id is not None]
        verified = (
            dict(
                (
                    await pdb.execute(
                        select(PartnerOrg.id, PartnerOrg.verification_status).where(
                            PartnerOrg.id.in_(org_ids)
                        )
                    )
                )
                .tuples()
                .all()
            )
            if org_ids
            else {}
        )
        for r in rows:
            pdb.expunge(r)
        for a in apps.values():
            pdb.expunge(a)
    return rows, apps, {k: v == "verified" for k, v in verified.items()}


async def _private_apps(tenant_id: uuid.UUID) -> list[App]:
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(App).where(
                        App.publisher_kind == "tenant",
                        App.owner_tenant_id == tenant_id,
                        App.status.in_(("development", "published")),
                    )
                )
            ).scalars()
        )
        for r in rows:
            pdb.expunge(r)
    return rows


async def all_cards(ctx: RequestContext, db: AsyncSession) -> list[ListingCardOut]:
    assert ctx.tenant_id is not None
    lk = await _lookups(ctx, db)
    rows, apps, verified = await _listing_rows()
    by_key = {r.connector_key: r for r in rows if r.connector_key}
    cards: list[ListingCardOut] = [_connector_card(lk, m, by_key.get(m.key)) for m in manifests()]
    for r in rows:
        if r.kind == "app" and r.app_id in apps:
            cards.append(
                _app_card(
                    lk,
                    apps[r.app_id],
                    r,
                    verified.get(r.partner_org_id, False) if r.partner_org_id else False,
                )
            )  # type: ignore[index, arg-type]
    cards.extend(_app_card(lk, a, None, False) for a in await _private_apps(ctx.tenant_id))
    cards.extend(_legacy_cards(lk))
    return cards


def _matches(card: ListingCardOut, q: ListingQuery) -> bool:
    if q.category and card.category != q.category:
        return False
    if q.publisher and card.publisher_kind != q.publisher:
        return False
    if q.installed is not None:
        installed = card.install_state != "not_installed"
        if installed != q.installed:
            return False
    if q.q:
        needle = q.q.strip().lower()
        hay = f"{card.name} {card.tagline} {CATEGORY_LABELS.get(card.category, card.category)}".lower()
        if needle not in hay:
            return False
    return True


async def list_listings(ctx: RequestContext, db: AsyncSession, q: ListingQuery) -> ListingPageOut:
    cards = [c for c in await all_cards(ctx, db) if _matches(c, q)]
    cards.sort(key=lambda c: (not c.featured, c.kind == "legacy_connector", c.name.lower()))
    start = (q.page - 1) * q.page_size
    return ListingPageOut(
        data=cards[start : start + q.page_size], total=len(cards), page=q.page, page_size=q.page_size
    )


def _flow_lines(flows: Any) -> list[DataFlowLineOut]:
    out: list[DataFlowLineOut] = []
    for f in flows or []:
        if not isinstance(f, dict):
            continue
        out.append(DataFlowLineOut(
            direction=str(f.get("direction") or "out"), entity=str(f.get("entity") or ""), fields=[str(x) for x in f.get("fields") or []],
            purpose=str(f.get("purpose") or ""), destination_country=str(f.get("destination_country") or "IN"),
            personal_data=bool(f.get("personal_data", True)),
        ))  # fmt: skip
    return out


async def _versions(listing_id: uuid.UUID) -> list[ListingVersionLineOut]:
    async with platform_session() as pdb:
        rows = (
            await pdb.execute(
                select(MarketplaceListingVersion)
                .where(
                    MarketplaceListingVersion.listing_id == listing_id,
                    MarketplaceListingVersion.status.in_(("published", "superseded", "suspended")),
                )
                .order_by(MarketplaceListingVersion.version_no.desc())
            )
        ).scalars()
        return [
            ListingVersionLineOut(version_no=r.version_no, published_at=r.published_at, changelog=r.changelog)
            for r in rows
        ]


async def _signed(keys: list[str]) -> list[str]:
    from app.core.storage import get_storage

    out: list[str] = []
    storage = get_storage()
    for key in keys:
        try:
            url = await storage.signed_url(key, 600)
        except Exception:
            url = None
        if url:
            out.append(url)
    return out


async def get_listing(ctx: RequestContext, db: AsyncSession, slug: str) -> ListingOut:
    assert ctx.tenant_id is not None
    lk = await _lookups(ctx, db)
    slug = slug.lower()
    legacy = by_slug(slug)
    if legacy is not None:
        card = next(c for c in _legacy_cards(lk) if c.slug == slug)
        return ListingOut(**card.model_dump(), description_md=legacy.tagline, supported_countries=["IN"])
    async with platform_session() as pdb:
        row = (
            await pdb.execute(select(MarketplaceListing).where(MarketplaceListing.slug == slug))
        ).scalar_one_or_none()
        if row is not None:
            pdb.expunge(row)
    if row is not None and row.publisher_kind == "tenant" and row.owner_tenant_id != ctx.tenant_id:
        row = None
    if row is not None and row.status not in ("published", "suspended", "delisted"):
        row = None
    connector = (
        find_manifest(row.connector_key)
        if row is not None and row.connector_key
        else find_manifest(slug.replace("-", "_"))
    )
    if connector is not None and (row is None or row.connector_key == connector.key):
        return await _connector_out(lk, connector, row)
    if row is not None and row.app_id is not None:
        async with platform_session() as pdb:
            app = await pdb.get(App, row.app_id)
            org = await pdb.get(PartnerOrg, row.partner_org_id) if row.partner_org_id else None
            verified = org is not None and org.verification_status == "verified"
            if app is not None:
                pdb.expunge(app)
        if app is not None:
            if row.status != "published" and app.id not in lk.by_app:
                raise NotFound("Listing not found", code="listing_not_found")
            return await _app_out(lk, app, row, verified)
    if slug.startswith(PRIVATE_PREFIX):
        app_slug = slug[len(PRIVATE_PREFIX) :]
        for app in await _private_apps(ctx.tenant_id):
            if str(app.slug).lower() == app_slug:
                return await _app_out(lk, app, None, False)
    raise NotFound("Listing not found", code="listing_not_found")


async def _connector_out(lk: _Ctx, m: ConnectorManifest, row: MarketplaceListing | None) -> ListingOut:
    card = _connector_card(lk, m, row)
    snap = platform_catalog.manifest_snapshot(m)
    return ListingOut(
        **card.model_dump(), description_md=platform_catalog._description(m),
        scopes=[ScopeLineOut(key=s, reason="Requested from the provider account", sensitive=False) for s in snap["oauth_scopes"]],
        data_flows=_flow_lines(snap["data_flows"]), egress_hosts=list(m.egress_hosts),
        supported_countries=list(row.supported_countries) if row is not None else ["IN"],
        pricing_note=row.pricing_note if row is not None else None,
        support_email=str(row.support_email) if row is not None and row.support_email else None,
        support_url=row.support_url if row is not None else m.docs_url,
        versions=await _versions(row.id) if row is not None else [],
        last_reviewed_at=row.last_reviewed_at if row is not None else None, connector_key=m.key,
        manifest_sha256=manifest_sha256(m), manifest_version=m.version, config_schema=m.config_model.model_json_schema(),
        secret_fields=snap["secret_fields"], legal_entity_scoped=m.legal_entity_scoped, erasure_supported=m.erasure,
        sync_jobs=list(snap["sync_jobs"]), auth=m.auth,
    )  # fmt: skip


async def _app_out(lk: _Ctx, app: App, row: MarketplaceListing | None, verified: bool) -> ListingOut:
    card = _app_card(lk, app, row, verified)
    catalogue = scopes()
    reasons = dict(app.scope_reasons or {})
    version = None
    if row is not None and row.current_version_id is not None:
        async with platform_session() as pdb:
            version = await pdb.get(MarketplaceListingVersion, row.current_version_id)
            if version is not None:
                pdb.expunge(version)
    return ListingOut(
        **card.model_dump(),
        description_md=(version.description_md if version is not None else str((app.manifest or {}).get("description") or "")),
        screenshots=await _signed(list(version.screenshots)) if version is not None else [],
        scopes=[ScopeLineOut(key=s, reason=str(reasons.get(s) or ""), sensitive=s in catalogue and is_sensitive(s)) for s in app.requested_scopes],
        data_flows=_flow_lines((app.manifest or {}).get("data_flows")),
        supported_countries=list(row.supported_countries) if row is not None else ["IN"],
        pricing_note=row.pricing_note if row is not None else None,
        support_email=(str(row.support_email) if row is not None and row.support_email else (str(app.support_email) if app.support_email else None)),
        support_url=row.support_url if row is not None else app.support_url,
        versions=await _versions(row.id) if row is not None else [],
        last_reviewed_at=row.last_reviewed_at if row is not None else None,
        app_id=app.id, manifest_sha256=app.manifest_sha256, manifest_version=app.manifest_version,
    )  # fmt: skip


__all__ = ["PRIVATE_PREFIX", "all_cards", "get_listing", "list_listings"]
