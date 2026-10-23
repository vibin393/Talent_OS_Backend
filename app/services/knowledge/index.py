# ruff: file-ignore[line-too-long]
"""Chunk index of Knowledge Sources (Epic 62 A1).

* ``build_chunks`` -- chunks a new version (inactive until published); embeds with the local
  ``ai.embeddings.embed(kind="passage")`` when the embedding backend is the in-process fake (tests / keyless dev),
  else leaves ``embedding`` NULL for the ``knowledge.reindex`` task (no Claude call, ever).
* ``sync_source`` -- the synchronous active-flag switch on publish / unpublish / archive / audience change.
* ``reconcile`` -- the ``knowledge-reconcile`` beat (every 5 minutes): re-asserts every chunk's ``active`` flag,
  audience and applicability from its source (backstop for the 5-minute unpublish AC).
* ``hybrid_search`` -- full-text ``ts_rank`` (OR of the query terms) + cosine over local embeddings, fused by
  reciprocal rank; only active chunks of published sources for the allowed audiences.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from loguru import logger
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.models.knowledge import KnowledgeChunk, KnowledgeSource, KnowledgeSourceVersion
from app.services.knowledge.chunking import chunk

RRF_K = 60
CANDIDATES = 50
MIN_COSINE = 0.25
_WORD = re.compile(r"[A-Za-z0-9]{2,40}")


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    version_no: int
    title: str
    heading: str | None
    text: str
    score: Decimal


def _embeddings_inline() -> bool:
    return str(get_settings().embedding_backend) == "fake"


def _embed(texts: Sequence[str], kind: str) -> list[list[float]] | None:
    try:
        from app.services.ai.embeddings import embed

        return embed(list(texts), kind=kind)  # type: ignore[arg-type]
    except Exception:  # pragma: no cover - no local model available
        logger.info("Knowledge embeddings unavailable; full-text search only")
        return None


def _model_name() -> str:
    from app.services.ai.embeddings import model_name

    return model_name()


async def build_chunks(db: AsyncSession, source: KnowledgeSource, version: KnowledgeSourceVersion) -> int:
    text_value = version.body_md if version.body_md is not None else (version.extracted_text or "")
    pieces = chunk(text_value)
    await db.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.source_id == source.id, KnowledgeChunk.version_no == version.version_no
        )
    )
    vectors = (
        _embed([f"{p.heading or ''} {p.text}".strip() for p in pieces], "passage")
        if pieces and _embeddings_inline()
        else None
    )
    model = _model_name() if vectors else None
    for seq, piece in enumerate(pieces, start=1):
        db.add(
            KnowledgeChunk(
                id=uuid.uuid4(), tenant_id=source.tenant_id, source_id=source.id, version_no=version.version_no,
                seq=seq, heading=piece.heading, text=piece.text, audience=source.audience,
                legal_entity_id=source.legal_entity_id, client_id=source.client_id, active=False,
                embedding=vectors[seq - 1] if vectors else None, embedding_model=model,
            )
        )  # fmt: skip
    await db.flush()
    return len(pieces)


async def sync_source(db: AsyncSession, source: KnowledgeSource) -> None:
    """Make exactly the published version's chunks active (synchronously, in the caller's transaction)."""
    live = source.status == "published" and source.published_version_no is not None
    await db.execute(
        update(KnowledgeChunk)
        .where(KnowledgeChunk.source_id == source.id)
        .values(
            active=(KnowledgeChunk.version_no == source.published_version_no) if live else False,
            audience=source.audience,
            legal_entity_id=source.legal_entity_id,
            client_id=source.client_id,
        )
        .execution_options(synchronize_session=False)
    )
    await db.flush()


RECONCILE_SQL = text(
    "UPDATE knowledge_chunks c SET active = (s.status = 'published' AND c.version_no = s.published_version_no), "
    "audience = s.audience, legal_entity_id = s.legal_entity_id, client_id = s.client_id "
    "FROM knowledge_sources s WHERE s.id = c.source_id AND s.tenant_id = c.tenant_id AND ("
    "c.active IS DISTINCT FROM (s.status = 'published' AND c.version_no = s.published_version_no) "
    "OR c.audience IS DISTINCT FROM s.audience OR c.legal_entity_id IS DISTINCT FROM s.legal_entity_id "
    "OR c.client_id IS DISTINCT FROM s.client_id)"
)


async def reconcile(db: AsyncSession) -> int:
    result = await db.execute(RECONCILE_SQL)
    return int(getattr(result, "rowcount", 0) or 0)


async def reindex(db: AsyncSession, source_id: uuid.UUID | None = None, batch: int = 200) -> int:
    """Embed chunks without an embedding (``knowledge.reindex``)."""
    stmt = select(KnowledgeChunk).where(KnowledgeChunk.embedding.is_(None)).limit(batch)
    if source_id is not None:
        stmt = stmt.where(KnowledgeChunk.source_id == source_id)
    rows = list((await db.execute(stmt)).scalars())
    if not rows:
        return 0
    vectors = _embed([f"{r.heading or ''} {r.text}".strip() for r in rows], "passage")
    if not vectors:
        return 0
    model = _model_name()
    for row, vec in zip(rows, vectors, strict=True):
        row.embedding = vec
        row.embedding_model = model
    await db.flush()
    return len(rows)


def or_query(query: str) -> str | None:
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query)))[:30]
    return " | ".join(words) if words else None


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    from app.services.ai.vector_index import cosine

    return float(cosine(a, b))


async def hybrid_search(
    db: AsyncSession,
    query: str,
    *,
    audiences: Sequence[str],
    limit: int = 5,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    applicable_only: bool = False,
) -> list[Hit]:
    base = (
        select(KnowledgeChunk, KnowledgeSource.title)
        .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
        .where(
            KnowledgeChunk.active.is_(True),
            KnowledgeChunk.audience.in_(list(audiences)),
            KnowledgeSource.status == "published",
            KnowledgeChunk.version_no == KnowledgeSource.published_version_no,
        )
    )
    if applicable_only:
        base = base.where(
            or_(KnowledgeChunk.legal_entity_id.is_(None), KnowledgeChunk.legal_entity_id == legal_entity_id),
            or_(KnowledgeChunk.client_id.is_(None), KnowledgeChunk.client_id == client_id),
        )
    ranked: dict[uuid.UUID, float] = {}
    found: dict[uuid.UUID, tuple[KnowledgeChunk, str]] = {}
    q = or_query(query)
    if q:
        tsq = func.to_tsquery("english", q)
        rank = func.ts_rank(KnowledgeChunk.text_tsv, tsq)
        rows = (
            await db.execute(
                base.add_columns(rank.label("rank"))
                .where(KnowledgeChunk.text_tsv.op("@@")(tsq))
                .order_by(rank.desc())
                .limit(CANDIDATES)
            )
        ).all()
        for pos, (row, title, _r) in enumerate(rows, start=1):
            ranked[row.id] = ranked.get(row.id, 0.0) + 1.0 / (RRF_K + pos)
            found[row.id] = (row, title)
    embedded = list((await db.execute(base.where(KnowledgeChunk.embedding.is_not(None)).limit(500))).all())
    if embedded:
        qvec = _embed([query], "query")
        if qvec:
            scored = sorted(
                ((row, title, _cos(qvec[0], row.embedding or [])) for row, title in embedded),
                key=lambda t: t[2], reverse=True,
            )  # fmt: skip
            pos = 0
            for row, title, score in scored[:CANDIDATES]:
                if score < MIN_COSINE:
                    break
                pos += 1
                ranked[row.id] = ranked.get(row.id, 0.0) + 1.0 / (RRF_K + pos)
                found[row.id] = (row, title)
    order = sorted(ranked.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    out: list[Hit] = []
    for chunk_id, score in order:
        row, title = found[chunk_id]
        out.append(
            Hit(
                chunk_id,
                row.source_id,
                row.version_no,
                title,
                row.heading,
                row.text,
                Decimal(str(round(score, 6))),
            )
        )
    return out


__all__ = ["Hit", "build_chunks", "hybrid_search", "or_query", "reconcile", "reindex", "sync_source"]
