# ruff: noqa: E501
"""Careers apply-source resolver for the ``job_network`` tag (Epic 75, spec A4; FR-388).

Registered with ``portal.apply.register_apply_source_resolver('job_network', resolve)`` from ``hooks.py``. The careers
apply flow (Epic 70's keyed registry) calls it with the posting Tenant's id and the ``ref`` from the apply link. It
confirms the listing belongs to this Tenant (any status, so a listing unlisted after the click still attributes) and
returns ``("job_network", <listing id>)``; a mismatched / malformed ref returns ``None`` and the source stays
``career_portal``. No candidate data is read or written -- a platform lookup of the public index only."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.models.job_network import NetworkListing


async def resolve(db: AsyncSession, tenant_id: uuid.UUID, ref: str | None) -> tuple[str, str] | None:  # noqa: ARG001 - db is the tenant session; the network reads only its own platform index
    if not ref:
        return None
    try:
        listing_id = uuid.UUID(str(ref))
    except (ValueError, AttributeError):
        return None
    async with platform_session() as pdb:
        row = (
            await pdb.execute(
                select(NetworkListing.id).where(
                    NetworkListing.id == listing_id, NetworkListing.tenant_id == tenant_id
                )
            )
        ).first()
    if row is None:
        return None
    return ("job_network", str(listing_id))


__all__ = ["resolve"]
