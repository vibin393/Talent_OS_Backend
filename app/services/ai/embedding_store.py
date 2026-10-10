"""Embedding storage for candidate / demand profiles (Epic 5, story 5.2). Depends only on core + the AI
package; callers hand in structured profiles (``app.services.embeddings_service`` gathers them).

Idempotent: a profile whose embedding text hashes to the stored ``content_hash`` (same model) is skipped.
After an upsert ``CandidateEmbeddingUpdated`` is emitted (dispatched after commit).
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.ai import CandidateEmbeddingUpdated
from app.schemas.matching_profiles import CandidateMatchProfile, DemandMatchProfile
from app.services.ai import embeddings
from app.services.ai.vector_index import get_vector_index


async def upsert_candidates(
    db: AsyncSession, tenant_id: uuid.UUID, profiles: Sequence[CandidateMatchProfile]
) -> list[uuid.UUID]:
    """Embed the profiles whose text changed. Returns the candidate ids that were (re-)embedded."""
    if not profiles:
        return []
    index = await get_vector_index(db)
    model = embeddings.model_name()
    texts = {p.candidate_id: embeddings.candidate_text(p) for p in profiles}
    hashes = {cid: embeddings.content_hash(t) for cid, t in texts.items()}
    stored = await index.hashes(db, "candidate", list(texts))
    todo = [cid for cid in texts if stored.get(cid) != f"{model}:{hashes[cid]}"]
    if not todo:
        return []
    vectors = embeddings.embed([texts[cid] for cid in todo], kind="passage")
    for cid, vec in zip(todo, vectors, strict=True):
        await index.upsert(db, tenant_id, "candidate", cid, vec, model=model, content_hash=hashes[cid])
        emit(db, CandidateEmbeddingUpdated(tenant_id=tenant_id, candidate_id=cid))
    return todo


async def delete_candidates(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_ids: Sequence[uuid.UUID]
) -> int:
    index = await get_vector_index(db)
    return await index.delete(db, tenant_id, "candidate", list(candidate_ids))


async def upsert_demand(db: AsyncSession, tenant_id: uuid.UUID, profile: DemandMatchProfile) -> list[float]:
    """Embed the demand profile (skipped when unchanged). Returns the stored vector."""
    index = await get_vector_index(db)
    model = embeddings.model_name()
    text = embeddings.demand_text(profile)
    digest = embeddings.content_hash(text)
    stored = await index.hashes(db, "demand", [profile.demand_id])
    if stored.get(profile.demand_id) == f"{model}:{digest}":
        existing = await index.vectors(db, "demand", [profile.demand_id])
        if profile.demand_id in existing:
            return existing[profile.demand_id]
    (vector,) = embeddings.embed([text], kind="passage")
    await index.upsert(db, tenant_id, "demand", profile.demand_id, vector, model=model, content_hash=digest)
    return vector


async def demand_vector(db: AsyncSession, demand_id: uuid.UUID) -> list[float] | None:
    index = await get_vector_index(db)
    return (await index.vectors(db, "demand", [demand_id])).get(demand_id)


async def candidate_vectors(
    db: AsyncSession, candidate_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[float]]:
    index = await get_vector_index(db)
    return await index.vectors(db, "candidate", list(candidate_ids))


async def search(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    vector: Sequence[float],
    candidate_ids_filter: Sequence[uuid.UUID] | None,
    k: int,
) -> list[tuple[uuid.UUID, float]]:
    index = await get_vector_index(db)
    return await index.search(db, tenant_id, vector, candidate_ids_filter, k)


async def search_text(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    candidate_ids_filter: Sequence[uuid.UUID] | None,
    k: int,
) -> list[tuple[uuid.UUID, float]]:
    """Semantic search: embed ``query`` (bge query prefix) and rank candidate profile vectors."""
    (vector,) = embeddings.embed([query], kind="query")
    return await search(db, tenant_id, vector, candidate_ids_filter, k)
