"""Embedding sync (Epic 5, story 5.2): gathers structured profiles from the candidates / demands services,
applies the tenant's proxy-field exclusions and hands them to the AI embedding store.

* ``sync_candidates(db, tenant_id, ids)`` -- idempotent: consented, live candidates are (re-)embedded when
  their embedding text changed; withdrawn-consent / archived / merged / erased candidates lose their vector.
* ``sync_demand(db, tenant_id, demand_id)`` -- embeds the demand profile (skipped when unchanged).
* ``backfill(db, tenant_id)`` -- every candidate in batches (CLI ``python -m app.services.ai.cli``).
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.matching_profiles import null_proxies
from app.services.ai import embedding_store
from app.services.candidates import search_port as candidates
from app.services.demands import matching_port as demands
from app.services.matching import settings as matching_settings

BATCH = 500


async def sync_candidates(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Sync embeddings of ``candidate_ids``; return the ids that were (re-)embedded."""
    ids = list(dict.fromkeys(candidate_ids))
    if not ids:
        return []
    proxies = await matching_settings.proxy_fields(db)
    changed: list[uuid.UUID] = []
    for start in range(0, len(ids), BATCH):
        chunk = ids[start : start + BATCH]
        consented = await candidates.ai_consent_active(db, chunk)
        profiles = [
            p
            for p in await candidates.get_matching_profiles(db, list(consented))
            if p.candidate_id in consented
        ]
        keep = {p.candidate_id for p in profiles}
        drop = [cid for cid in chunk if cid not in keep]
        if drop:
            await embedding_store.delete_candidates(db, tenant_id, drop)
        profiles = [null_proxies(p, proxies) for p in profiles]
        changed += await embedding_store.upsert_candidates(db, tenant_id, profiles)
    return changed


async def delete_candidates(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_ids: Sequence[uuid.UUID]
) -> int:
    return await embedding_store.delete_candidates(db, tenant_id, list(candidate_ids))


async def sync_demand(db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID) -> list[float] | None:
    profile = await demands.get_matching_profile(db, demand_id)
    if profile is None:
        return None
    return await embedding_store.upsert_demand(db, tenant_id, profile)


async def backfill(db: AsyncSession, tenant_id: uuid.UUID, *, batch: int = 1000) -> int:
    total = 0
    after: uuid.UUID | None = None
    while True:
        ids = await candidates.live_ids(db, after=after, limit=batch)
        if not ids:
            break
        total += len(await sync_candidates(db, tenant_id, ids))
        after = ids[-1]
        await db.commit()
    for demand_id in await demands.list_open_demand_ids(db):
        await sync_demand(db, tenant_id, demand_id)
    return total
