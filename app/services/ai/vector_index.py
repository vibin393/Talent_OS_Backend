"""Vector index port (Epic 5, AD-7). Callers only use ``VectorIndex`` -- never pgvector SQL directly.

* ``PgVectorIndex`` -- ``ORDER BY vec <=> :v LIMIT k`` (HNSW, cosine) when the ``vector`` extension and the
  ``vec`` columns exist (migration 20261001_52 adds them only where pgvector is available).
* ``ArrayVectorIndex`` -- ``float4[]`` rows pre-filtered in SQL (tenant via RLS + optional id filter), loaded
  in chunks of 20k into numpy, dot product (vectors are L2-normalised, so dot = cosine) and ``argpartition``.
  A per-process LRU keyed by ``(tenant_id, max(updated_at), count)`` keeps the tenant matrix for unfiltered
  or large-filter searches.

``get_vector_index(db)`` picks the implementation once per process and logs which one. Every method runs on
the caller's RLS-scoped session. Candidates without AI consent have no embedding rows (deleted on
withdrawal), so they never appear in results.
"""

import threading
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import numpy as np
from loguru import logger
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.models.embeddings import CandidateEmbedding, DemandEmbedding

OwnerKind = Literal["candidate", "demand"]
CHUNK = 20_000
DIRECT_FILTER_MAX = 20_000
CACHE_TENANTS = 4


class VectorIndex(Protocol):
    name: str

    async def upsert(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        owner_kind: OwnerKind,
        owner_id: uuid.UUID,
        vector: Sequence[float],
        *,
        model: str,
        content_hash: str,
    ) -> None: ...

    async def delete(
        self, db: AsyncSession, tenant_id: uuid.UUID, owner_kind: OwnerKind, owner_ids: Sequence[uuid.UUID]
    ) -> int: ...

    async def search(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        vector: Sequence[float],
        candidate_ids_filter: Sequence[uuid.UUID] | None,
        k: int,
    ) -> list[tuple[uuid.UUID, float]]: ...

    async def vectors(
        self, db: AsyncSession, owner_kind: OwnerKind, owner_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[float]]: ...

    async def hashes(
        self, db: AsyncSession, owner_kind: OwnerKind, owner_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]: ...


def _model_of(kind: OwnerKind) -> Any:
    return CandidateEmbedding if kind == "candidate" else DemandEmbedding


def _id_col(kind: OwnerKind) -> Any:
    return CandidateEmbedding.candidate_id if kind == "candidate" else DemandEmbedding.demand_id


class _BaseIndex:
    name = "base"
    uses_vec_column = False

    async def upsert(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        owner_kind: OwnerKind,
        owner_id: uuid.UUID,
        vector: Sequence[float],
        *,
        model: str,
        content_hash: str,
    ) -> None:
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "tenant_id": tenant_id,
            "vector": [float(x) for x in vector],
            "model": model,
            "content_hash": content_hash,
            "updated_at": now,
        }
        if owner_kind == "candidate":
            stmt = insert(CandidateEmbedding).values(candidate_id=owner_id, kind="profile", **values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["candidate_id", "kind"],
                set_={k: stmt.excluded[k] for k in ("vector", "model", "content_hash", "updated_at")},
            )
        else:
            stmt = insert(DemandEmbedding).values(demand_id=owner_id, **values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["demand_id"],
                set_={k: stmt.excluded[k] for k in ("vector", "model", "content_hash", "updated_at")},
            )
        await db.execute(stmt)
        if self.uses_vec_column:
            table = "candidate_embeddings" if owner_kind == "candidate" else "demand_embeddings"
            col = "candidate_id" if owner_kind == "candidate" else "demand_id"
            await db.execute(
                text(f"UPDATE {table} SET vec = CAST(:v AS vector) WHERE {col} = :id"),  # nosec B608 - table/column identifiers are in-code constants (whitelisted); values are bound params
                {"v": "[" + ",".join(repr(float(x)) for x in vector) + "]", "id": owner_id},
            )
        _cache_invalidate(tenant_id)

    async def delete(
        self, db: AsyncSession, tenant_id: uuid.UUID, owner_kind: OwnerKind, owner_ids: Sequence[uuid.UUID]
    ) -> int:
        if not owner_ids:
            return 0
        model = _model_of(owner_kind)
        result = await db.execute(delete(model).where(_id_col(owner_kind).in_(list(owner_ids))))
        _cache_invalidate(tenant_id)
        return int(result.rowcount or 0)

    async def vectors(
        self, db: AsyncSession, owner_kind: OwnerKind, owner_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[float]]:
        if not owner_ids:
            return {}
        model = _model_of(owner_kind)
        rows = await db.execute(
            select(_id_col(owner_kind), model.vector).where(_id_col(owner_kind).in_(list(owner_ids)))
        )
        return {r[0]: list(r[1]) for r in rows}

    async def hashes(
        self, db: AsyncSession, owner_kind: OwnerKind, owner_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not owner_ids:
            return {}
        model = _model_of(owner_kind)
        rows = await db.execute(
            select(_id_col(owner_kind), model.content_hash, model.model).where(
                _id_col(owner_kind).in_(list(owner_ids))
            )
        )
        return {r[0]: f"{r[2]}:{r[1]}" for r in rows}


# ================================================================ ArrayVectorIndex
_cache: "OrderedDict[uuid.UUID, tuple[tuple[Any, int], list[uuid.UUID], np.ndarray]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_invalidate(tenant_id: uuid.UUID) -> None:
    with _cache_lock:
        _cache.pop(tenant_id, None)


def _top_k(
    ids: list[uuid.UUID], matrix: np.ndarray, query: np.ndarray, k: int
) -> list[tuple[uuid.UUID, float]]:
    if not ids or k <= 0:
        return []
    scores = matrix @ query
    k = min(k, len(ids))
    part = np.argpartition(-scores, k - 1)[:k] if k < len(ids) else np.arange(len(ids))
    order = part[np.argsort(-scores[part], kind="stable")]
    return [(ids[i], float(scores[i])) for i in order]


class ArrayVectorIndex(_BaseIndex):
    name = "ArrayVectorIndex"

    async def _load(
        self, db: AsyncSession, id_filter: Sequence[uuid.UUID] | None
    ) -> tuple[list[uuid.UUID], np.ndarray]:
        stmt = select(CandidateEmbedding.candidate_id, CandidateEmbedding.vector).where(
            CandidateEmbedding.kind == "profile"
        )
        if id_filter is not None:
            stmt = stmt.where(CandidateEmbedding.candidate_id.in_(list(id_filter)))
        ids: list[uuid.UUID] = []
        chunks: list[np.ndarray] = []
        result = await db.stream(stmt.execution_options(yield_per=CHUNK))
        async for part in result.partitions(CHUNK):
            ids.extend(r[0] for r in part)
            chunks.append(np.asarray([r[1] for r in part], dtype=np.float32))
        dim = get_settings().embedding_dim
        matrix = np.vstack(chunks) if chunks else np.zeros((0, dim), dtype=np.float32)
        return ids, matrix

    async def _tenant_matrix(
        self, db: AsyncSession, tenant_id: uuid.UUID
    ) -> tuple[list[uuid.UUID], np.ndarray]:
        stamp_row = (
            await db.execute(
                select(func.max(CandidateEmbedding.updated_at), func.count()).select_from(CandidateEmbedding)
            )
        ).one()
        key = (stamp_row[0], int(stamp_row[1]))
        with _cache_lock:
            hit = _cache.get(tenant_id)
            if hit is not None and hit[0] == key:
                _cache.move_to_end(tenant_id)
                return hit[1], hit[2]
        ids, matrix = await self._load(db, None)
        with _cache_lock:
            _cache[tenant_id] = (key, ids, matrix)
            _cache.move_to_end(tenant_id)
            while len(_cache) > CACHE_TENANTS:
                _cache.popitem(last=False)
        return ids, matrix

    async def search(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        vector: Sequence[float],
        candidate_ids_filter: Sequence[uuid.UUID] | None,
        k: int,
    ) -> list[tuple[uuid.UUID, float]]:
        query = np.asarray(vector, dtype=np.float32)
        if candidate_ids_filter is not None:
            wanted = list(dict.fromkeys(candidate_ids_filter))
            if not wanted:
                return []
            if len(wanted) <= DIRECT_FILTER_MAX:
                ids, matrix = await self._load(db, wanted)
                return _top_k(ids, matrix, query, k)
            all_ids, full = await self._tenant_matrix(db, tenant_id)
            keep = set(wanted)
            rows = [i for i, cid in enumerate(all_ids) if cid in keep]
            return _top_k([all_ids[i] for i in rows], full[rows], query, k)
        ids, matrix = await self._tenant_matrix(db, tenant_id)
        return _top_k(ids, matrix, query, k)


# ================================================================ PgVectorIndex
class PgVectorIndex(_BaseIndex):
    name = "PgVectorIndex"
    uses_vec_column = True

    async def search(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,  # ruff: ignore[unused-method-argument] - RLS scopes the session
        vector: Sequence[float],
        candidate_ids_filter: Sequence[uuid.UUID] | None,
        k: int,
    ) -> list[tuple[uuid.UUID, float]]:
        literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
        where = "kind = 'profile' AND vec IS NOT NULL"
        params: dict[str, Any] = {"v": literal, "k": int(k)}
        if candidate_ids_filter is not None:
            if not candidate_ids_filter:
                return []
            where += " AND candidate_id = ANY(:ids)"
            params["ids"] = list(candidate_ids_filter)
        rows = await db.execute(
            text(
                "SELECT candidate_id, 1 - (vec <=> CAST(:v AS vector)) AS cosine FROM candidate_embeddings "  # nosec B608 - table/column identifiers are in-code constants (whitelisted); values are bound params
                f"WHERE {where} ORDER BY vec <=> CAST(:v AS vector) LIMIT :k"
            ),
            params,
        )
        return [(r[0], float(r[1])) for r in rows]


# ================================================================ selection
_selected: VectorIndex | None = None


async def _pgvector_ready(db: AsyncSession) -> bool:
    ext = (await db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))).first()
    if ext is None:
        return False
    col = (
        await db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'candidate_embeddings' AND column_name = 'vec'"
            )
        )
    ).first()
    return col is not None


async def get_vector_index(db: AsyncSession) -> VectorIndex:
    """Return the process-wide index implementation (chosen on first use, logged)."""
    global _selected  # process-wide choice, made once
    if _selected is not None:
        return _selected
    wanted = get_settings().vector_index
    if wanted == "array":
        chosen: VectorIndex = ArrayVectorIndex()
    elif wanted == "pgvector" or await _pgvector_ready(db):
        chosen = PgVectorIndex()
    else:
        chosen = ArrayVectorIndex()
    logger.info("Vector index: {} selected", chosen.name)
    _selected = chosen
    return chosen


def set_vector_index(index: VectorIndex | None) -> None:
    """Force an implementation (tests); ``None`` re-runs the selection on next use."""
    global _selected  # test hook
    _selected = index


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(va @ vb / (na * nb))
