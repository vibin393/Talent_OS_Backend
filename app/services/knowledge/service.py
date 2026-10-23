# ruff: file-ignore[line-too-long]
"""Knowledge search (Epic 53 contract stub, filled by Epic 62 A1; the signature is pinned by
``tests/contracts/signatures.json``).

``search`` requires ``knowledge:read``; returns only active chunks of published sources whose audience is the requested
one or ``both`` (``internal`` -> ``internal`` / ``both``). When the caller is the onboarding agent
(``ctx.agent_key == "onboarding"``) the audience ``internal`` is refused (empty result) and the hits are narrowed to the
asker's legal entity / client (the thread bound to the run), so the Q&A agent can never read internal or other
employers' material. Citations are ``kb:<chunk_id>``; the excerpt is at most 600 characters.
"""

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.core.permissions import Principal
from app.models.knowledge import KnowledgeChunk, KnowledgeSource
from app.schemas.knowledge import KnowledgeHit
from app.services.knowledge import index
from app.services.knowledge.common import AGENT_KEY, EXCERPT_CHARS

CITATION_PREFIX = "kb:"


def audiences_for(audience: str) -> tuple[str, ...]:
    return (audience, "both") if audience != "both" else ("worker", "candidate", "both")


async def _asker_scope(
    db: AsyncSession, run_id: uuid.UUID | None
) -> tuple[uuid.UUID | None, uuid.UUID | None] | None:
    """(legal_entity_id, client_id) of the thread's Placement for an onboarding run, else None."""
    if run_id is None:
        return None
    from app.services.knowledge import qa

    thread = await qa.thread_for_run(db, run_id)
    if thread is None:
        return (None, None)
    return await qa.applicability(db, thread)


async def search(
    ctx: RequestContext,
    db: AsyncSession,
    query: str,
    *,
    audience: Literal["worker", "candidate", "internal"],
    limit: int = 5,
) -> list[KnowledgeHit]:
    """Search the tenant knowledge sources for an audience; hits carry citations."""
    principal = ctx.principal
    if not isinstance(principal, Principal) or not principal.has("knowledge:read"):
        raise Forbidden("You cannot read knowledge sources", code="forbidden")
    agent = ctx.agent_key == AGENT_KEY
    if agent and audience == "internal":
        return []
    scope = await _asker_scope(db, ctx.agent_run_id) if agent else None
    hits = await index.hybrid_search(
        db, query, audiences=audiences_for(audience), limit=max(1, min(limit, 10)),
        legal_entity_id=scope[0] if scope else None, client_id=scope[1] if scope else None,
        applicable_only=scope is not None,
    )  # fmt: skip
    return [
        KnowledgeHit(
            source_id=h.source_id,
            source_version=h.version_no,
            title=h.title,
            excerpt=h.text[:EXCERPT_CHARS],
            citation=f"{CITATION_PREFIX}{h.chunk_id}",
            score=h.score,
        )
        for h in hits
    ]


async def quick_search(
    ctx: RequestContext, db: AsyncSession, query: str, *, limit: int = 5
) -> list[KnowledgeHit]:
    """C-16 (Epic 65 / Copilot): a quick internal search across every published source an internal user may read
    (audience ``internal`` / ``both`` / ``worker`` / ``candidate``); needs ``knowledge:read``."""
    principal = ctx.principal
    if not isinstance(principal, Principal) or not principal.has("knowledge:read"):
        raise Forbidden("You cannot read knowledge sources", code="forbidden")
    if ctx.agent_key == AGENT_KEY:
        return []
    hits = await index.hybrid_search(
        db, query, audiences=("internal", "both", "worker", "candidate"), limit=max(1, min(limit, 10))
    )
    return [
        KnowledgeHit(
            source_id=h.source_id,
            source_version=h.version_no,
            title=h.title,
            excerpt=h.text[:EXCERPT_CHARS],
            citation=f"{CITATION_PREFIX}{h.chunk_id}",
            score=h.score,
        )
        for h in hits
    ]


async def active_chunks(
    db: AsyncSession, chunk_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[KnowledgeChunk, str]]:
    """Active chunks of published sources by id (with the source title) -- used to validate citations."""
    if not chunk_ids:
        return {}
    rows = (
        await db.execute(
            select(KnowledgeChunk, KnowledgeSource.title)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
            .where(
                KnowledgeChunk.id.in_(chunk_ids),
                KnowledgeChunk.active.is_(True),
                KnowledgeSource.status == "published",
                KnowledgeChunk.version_no == KnowledgeSource.published_version_no,
            )
        )
    ).all()
    return {row.id: (row, title) for row, title in rows}


def parse_citation(value: str) -> tuple[str, uuid.UUID] | None:
    kind, _, raw = value.partition(":")
    if kind not in ("kb", "task", "case"):
        return None
    try:
        return kind, uuid.UUID(raw)
    except ValueError:
        return None


__all__ = ["CITATION_PREFIX", "active_chunks", "audiences_for", "parse_citation", "quick_search", "search"]
