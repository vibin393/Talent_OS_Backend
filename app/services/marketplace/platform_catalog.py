# ruff: noqa: E501, D401
"""Manifest -> listing sync (Epic 69, spec A3; AD-81). Every registered Connector Manifest gets a platform listing
(``kind='connector'``, or ``calendar`` for listing-only calendar manifests) and, when its ``manifest_sha256`` changes, a
new ``published`` version (platform-authored: no partner review). The previous published version is ``superseded``.

The version rows also keep every consented manifest snapshot, so re-consent can diff an Installation's consented
manifest against the current one (``consented_manifest``) and adapters keep the consented egress hosts until upgrade.
"""

import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.integrations import ConnectorManifest, DataFlow, OAuthProviderSpec, manifest_sha256, manifests
from app.models.marketplace import MarketplaceListing, MarketplaceListingVersion
from app.services.marketplace.legacy_catalog import listing_category

CALENDAR_MANAGE_PATH = "/settings/connections"


def _now() -> datetime:
    return datetime.now(UTC)


def slug_for(key: str) -> str:
    return key.replace("_", "-")[:60]


def flow_dict(f: DataFlow) -> dict[str, Any]:
    return {
        "direction": f.direction, "entity": f.entity, "fields": list(f.fields), "purpose": f.purpose,
        "destination_country": f.destination_country, "personal_data": f.personal_data,
    }  # fmt: skip


def manifest_snapshot(m: ConnectorManifest) -> dict[str, Any]:
    """The manifest as consented (no factories / classes): what ``manifest_sha256`` covers plus display fields."""
    return {
        "key": m.key,
        "version": m.version,
        "name": m.name,
        "summary": m.summary,
        "category": m.category,
        "capabilities": sorted(m.capabilities),
        "auth": m.auth,
        "oauth_scopes": sorted(m.oauth.scopes) if m.oauth else [],
        "secret_fields": [
            {"name": s.name, "label": s.label, "kind": s.kind, "required": s.required}
            for s in m.secret_fields
        ],
        "data_flows": [flow_dict(f) for f in m.data_flows],
        "egress_hosts": sorted(m.egress_hosts),
        "sync_jobs": [
            {"key": j.key, "capability": j.capability, "direction": j.direction, "trigger": j.trigger}
            for j in m.sync_jobs
        ],
        "webhook": m.webhook.scheme if m.webhook else None,
        "erasure": m.erasure,
        "legal_entity_scoped": m.legal_entity_scoped,
        "docs_url": m.docs_url,
    }


def manifest_from_snapshot(current: ConnectorManifest, snap: dict[str, Any]) -> ConnectorManifest:
    """Rebuild the consent-relevant shape of an older manifest version on top of ``current`` (for ``manifest_diff``)."""
    scopes = tuple(snap.get("oauth_scopes") or ())
    oauth: OAuthProviderSpec | None
    if current.oauth is not None:
        oauth = dataclasses.replace(current.oauth, scopes=scopes)
    else:
        oauth = OAuthProviderSpec(authorize_url="", token_url="", scopes=scopes) if scopes else None
    flows = tuple(
        DataFlow(
            direction=f["direction"], entity=f["entity"], fields=tuple(f.get("fields") or ()), purpose=f["purpose"],
            destination_country=f.get("destination_country", "IN"), personal_data=bool(f.get("personal_data")),
        )
        for f in snap.get("data_flows") or []
    )  # fmt: skip
    return dataclasses.replace(
        current,
        version=int(snap.get("version") or current.version),
        capabilities=frozenset(snap.get("capabilities") or ()),
        auth=snap.get("auth") or current.auth,
        oauth=oauth,
        data_flows=flows,
        egress_hosts=tuple(snap.get("egress_hosts") or ()),
    )


def _description(m: ConnectorManifest) -> str:
    text = m.summary.strip()
    if m.docs_url:
        text += f"\n\n[Documentation]({m.docs_url})"
    return text[:8000]


async def _upsert(pdb: AsyncSession, m: ConnectorManifest) -> tuple[MarketplaceListing, bool]:
    """Upsert the listing and, when the digest is new, a published version. Returns (listing, version_added)."""
    digest = manifest_sha256(m)
    kind = "calendar" if m.category == "calendar" else "connector"
    listing = (
        await pdb.execute(
            select(MarketplaceListing).where(MarketplaceListing.connector_key == m.key).with_for_update()
        )
    ).scalar_one_or_none()
    if listing is None:
        listing = MarketplaceListing(
            id=uuid.uuid4(), slug=slug_for(m.key), kind=kind, connector_key=m.key, publisher_kind="platform",
            category=listing_category(m.category), name=m.name[:120], tagline=(m.summary or m.name)[:200],
            status="published", supported_countries=["IN"],
            manage_path=CALENDAR_MANAGE_PATH if kind == "calendar" else None,
        )  # fmt: skip
        pdb.add(listing)
        await pdb.flush()
    else:
        listing.name, listing.tagline = m.name[:120], (m.summary or m.name)[:200]
        listing.category = listing_category(m.category)
    existing = (
        await pdb.execute(
            select(MarketplaceListingVersion).where(
                MarketplaceListingVersion.listing_id == listing.id,
                MarketplaceListingVersion.manifest_sha256 == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if listing.current_version_id is None:
            listing.current_version_id = existing.id
        return listing, False
    now = _now()
    await pdb.execute(
        update(MarketplaceListingVersion)
        .where(
            MarketplaceListingVersion.listing_id == listing.id,
            MarketplaceListingVersion.status == "published",
        )
        .values(status="superseded")
    )
    next_no = (
        await pdb.execute(
            select(func.coalesce(func.max(MarketplaceListingVersion.version_no), 0)).where(
                MarketplaceListingVersion.listing_id == listing.id
            )
        )
    ).scalar_one() + 1
    version = MarketplaceListingVersion(
        id=uuid.uuid4(), listing_id=listing.id, version_no=next_no, manifest_version=m.version, manifest_sha256=digest,
        manifest_snapshot=manifest_snapshot(m), scopes=[], data_flows=[flow_dict(f) for f in m.data_flows],
        egress_hosts=sorted(m.egress_hosts), description_md=_description(m), screenshots=[], status="published",
        submitted_by_kind="platform", submitted_at=now, published_at=now,
    )  # fmt: skip
    pdb.add(version)
    await pdb.flush()
    listing.current_version_id = version.id
    listing.status = "published" if listing.status == "draft" else listing.status
    await pdb.flush()
    return listing, True


async def ensure_version(m: ConnectorManifest) -> None:
    """Record the manifest being consented to (install / upgrade) so later diffs find it."""
    async with platform_session() as pdb:
        await _upsert(pdb, m)


async def sync() -> list[str]:
    """Upsert every manifest's listing; returns the connector keys that got a new version (task
    ``marketplace.sync_platform_catalog``)."""
    changed: list[str] = []
    async with platform_session() as pdb:
        for m in manifests():
            _, added = await _upsert(pdb, m)
            if added:
                changed.append(m.key)
    return changed


async def snapshot_for(connector_key: str, digest: str) -> dict[str, Any] | None:
    async with platform_session() as pdb:
        row = (
            await pdb.execute(
                select(MarketplaceListingVersion.manifest_snapshot)
                .join(MarketplaceListing, MarketplaceListing.id == MarketplaceListingVersion.listing_id)
                .where(
                    MarketplaceListing.connector_key == connector_key,
                    MarketplaceListingVersion.manifest_sha256 == digest,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    return dict(row) if row is not None else None


async def consented_manifest(current: ConnectorManifest, digest: str) -> ConnectorManifest | None:
    """The consented manifest shape for ``digest`` (None when unknown)."""
    if digest == manifest_sha256(current):
        return current
    snap = await snapshot_for(current.key, digest)
    return manifest_from_snapshot(current, snap) if snap is not None else None


__all__ = [
    "consented_manifest",
    "ensure_version",
    "flow_dict",
    "manifest_from_snapshot",
    "manifest_snapshot",
    "slug_for",
    "snapshot_for",
    "sync",
]
