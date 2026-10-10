# ruff: noqa: E501
"""Platform index writer/reader (Epic 75, spec A3). The **only** writers of ``network_listings`` -- always in a
``platform_session``. The caller (sync task) has already projected the tenant's careers-public data; the index never
reads tenant data itself."""

import dataclasses
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_network import NetworkListing
from app.services.job_network.constants import SEARCH_CONFIG
from app.services.job_network.listings import NetworkListingData

_BLANK_STATUSES = ("unlisted", "removed")


@dataclasses.dataclass(frozen=True, slots=True)
class UpsertResult:
    listing_id: uuid.UUID
    changed: bool  # content hash changed (or new row) -> needs re-check
    status: str


async def _recompute_tsv(pdb: AsyncSession, listing_id: uuid.UUID) -> None:
    # Vector = title(A) || skills(A) || client_name(B) || description_text(C), ``simple`` config.
    await pdb.execute(
        text(
            "UPDATE network_listings SET search_tsv = "
            "setweight(to_tsvector(:cfg, coalesce(title,'')), 'A') || "
            "setweight(to_tsvector(:cfg, coalesce(array_to_string(skills, ' '), '')), 'A') || "
            "setweight(to_tsvector(:cfg, coalesce(client_name,'')), 'B') || "
            "setweight(to_tsvector(:cfg, coalesce(description_text,'')), 'C') "
            "WHERE id = :id"
        ),
        {"cfg": SEARCH_CONFIG, "id": str(listing_id)},
    )


async def upsert(pdb: AsyncSession, *, member_id: uuid.UUID, data: NetworkListingData) -> UpsertResult:
    """Insert or update the listing for ``(tenant_id, posting_ref)``. An unchanged ``content_sha256`` on an already
    listed/held row short-circuits (no re-check). Returns whether a re-check is needed."""
    existing = (
        await pdb.execute(
            select(NetworkListing)
            .where(
                NetworkListing.tenant_id == data.tenant_id,
                NetworkListing.posting_ref == data.posting_ref,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if existing is None:
        listing_id = uuid.uuid4()
        apply_url = f"{data.apply_url}?src=job_network&ref={listing_id}"
        pdb.add(
            NetworkListing(
                id=listing_id, member_id=member_id, tenant_id=data.tenant_id, posting_ref=data.posting_ref,
                status="pending_checks", hold_reasons=[], title=data.title, description_html=data.description_html,
                description_text=data.description_text, client_name=data.client_name, city=data.city,
                region=data.region, country=data.country, location_hidden=data.location_hidden,
                work_mode=data.work_mode, employment_type=data.employment_type, experience_min=data.experience_min,
                experience_max=data.experience_max, skills=list(data.skills), category=data.category,
                pay_min=data.pay_min, pay_max=data.pay_max, pay_currency=data.pay_currency,
                pay_period=data.pay_period, apply_url=apply_url, posting_url=data.posting_url,
                published_at=data.published_at, valid_through=data.valid_through,
                content_sha256=data.content_sha256, synced_at=now,
            )
        )
        await pdb.flush()
        await _recompute_tsv(pdb, listing_id)
        return UpsertResult(listing_id, changed=True, status="pending_checks")

    unchanged = existing.content_sha256 == data.content_sha256 and existing.status in ("listed", "held")
    existing.member_id = member_id
    existing.title = data.title
    existing.description_html = data.description_html
    existing.description_text = data.description_text
    existing.client_name = data.client_name
    existing.city = data.city
    existing.region = data.region
    existing.country = data.country
    existing.location_hidden = data.location_hidden
    existing.work_mode = data.work_mode
    existing.employment_type = data.employment_type
    existing.experience_min = data.experience_min
    existing.experience_max = data.experience_max
    existing.skills = list(data.skills)
    existing.category = data.category
    existing.pay_min = data.pay_min
    existing.pay_max = data.pay_max
    existing.pay_currency = data.pay_currency
    existing.pay_period = data.pay_period
    existing.apply_url = f"{data.apply_url}?src=job_network&ref={existing.id}"
    existing.posting_url = data.posting_url
    existing.published_at = data.published_at
    existing.valid_through = data.valid_through
    existing.synced_at = now
    if not unchanged:
        existing.content_sha256 = data.content_sha256
        existing.status = "pending_checks"
        existing.hold_reasons = []
    await pdb.flush()
    await _recompute_tsv(pdb, existing.id)
    return UpsertResult(existing.id, changed=not unchanged, status=existing.status)


async def set_decision(
    pdb: AsyncSession, listing_id: uuid.UUID, *, status: str, hold_reasons: list[str]
) -> None:
    """Record the outcome of the automated checks (``listed`` or ``held``)."""
    values: dict[str, object] = {"status": status, "hold_reasons": hold_reasons}
    if status == "listed":
        values["listed_at"] = func.coalesce(NetworkListing.listed_at, func.now())
    await pdb.execute(update(NetworkListing).where(NetworkListing.id == listing_id).values(**values))


async def unlist(pdb: AsyncSession, *, tenant_id: uuid.UUID, posting_ref: uuid.UUID, reason: str) -> bool:  # noqa: ARG001 - reason documents the caller's intent
    """Unlist one posting of a tenant and blank its text (DPDP: no stale public text). Terminal ``removed`` rows are
    left untouched."""
    listing = (
        await pdb.execute(
            select(NetworkListing)
            .where(NetworkListing.tenant_id == tenant_id, NetworkListing.posting_ref == posting_ref)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if listing is None or listing.status == "removed":
        return False
    _blank(listing, status="unlisted")
    listing.hold_reasons = []
    await pdb.flush()
    await pdb.execute(
        update(NetworkListing).where(NetworkListing.id == listing.id).values(search_tsv=text("''::tsvector"))
    )
    return True


def _blank(listing: NetworkListing, *, status: str) -> None:
    listing.status = status
    listing.description_html = ""
    listing.description_text = ""


async def unlist_tenant(pdb: AsyncSession, tenant_id: uuid.UUID, *, reason: str) -> int:  # noqa: ARG001 - reason documents the caller's intent
    """Unlist every (non-terminal) listing of a tenant and blank text + search vector in one statement."""
    result = await pdb.execute(
        update(NetworkListing)
        .where(NetworkListing.tenant_id == tenant_id, NetworkListing.status != "removed")
        .values(
            status="unlisted", hold_reasons=[], description_html="", description_text="",
            search_tsv=text("''::tsvector"), synced_at=func.now(),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["UpsertResult", "set_decision", "unlist", "unlist_tenant", "upsert"]
