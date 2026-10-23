"""Search / embeddings / matching erasure (Epic 5 for Epic 12, FR-68): handler ``search.index`` (70).

Erasure deletes the candidate's (and merged records') search projection row, profile embedding and matches;
the rows are derived data and are rebuilt from nothing. ``verify`` reports any row left behind.
Consent withdrawal (``ai_processing``) purges the AI-derived rows (embeddings + matches) through
``register_ai_derived_purger``; the keyword-search row stays (withdrawn candidates remain findable).
"""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.embeddings import CandidateEmbedding
from app.models.matching import Match
from app.models.search import CandidateSearchDoc
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_ai_derived_purger, register_erasure_handler


async def _delete(db: AsyncSession, model: Any, column: Any, ids: list[uuid.UUID]) -> int:
    result = await db.execute(delete(model).where(column.in_(ids)))
    return int(result.rowcount or 0)


class SearchIndexHandler:
    name = "search.index"
    order = 70
    tables = ("candidate_search_docs", "candidate_embeddings", "matches")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        return {
            "candidate_search_docs": await _delete(
                db, CandidateSearchDoc, CandidateSearchDoc.candidate_id, ids
            ),
            "candidate_embeddings": await _delete(
                db, CandidateEmbedding, CandidateEmbedding.candidate_id, ids
            ),
            "matches": await _delete(db, Match, Match.candidate_id, ids),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        ids = await subject_ids(db, ctx.candidate_id)
        rows = (await db.execute(select(Match).where(Match.candidate_id.in_(ids)))).scalars()
        return {
            "matches": [
                {
                    "demand_id": m.demand_id,
                    "overall": m.overall,
                    "sub_scores": m.sub_scores,
                    "explanation": m.explanation,
                    "computed_at": m.computed_at,
                    "dismiss_reason": m.dismiss_reason,
                }
                for m in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        for model, column in (
            (CandidateSearchDoc, CandidateSearchDoc.candidate_id),
            (CandidateEmbedding, CandidateEmbedding.candidate_id),
            (Match, Match.candidate_id),
        ):
            left = (await db.execute(select(column).where(column.in_(ids)).limit(1))).first()
            if left is not None:
                findings.append(f"{model.__tablename__} row remains")
        return findings


async def purge_ai_derived(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, int]:
    return {
        "candidate_embeddings": await _delete(
            db, CandidateEmbedding, CandidateEmbedding.candidate_id, [candidate_id]
        ),
        "matches": await _delete(db, Match, Match.candidate_id, [candidate_id]),
    }


async def find_ai_derived(db: AsyncSession, candidate_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = list(candidate_ids)
    if not ids:
        return set()
    found = {
        r[0]
        for r in await db.execute(
            select(CandidateEmbedding.candidate_id).where(CandidateEmbedding.candidate_id.in_(ids))
        )
    }
    found |= {r[0] for r in await db.execute(select(Match.candidate_id).where(Match.candidate_id.in_(ids)))}
    return found


register_erasure_handler(SearchIndexHandler())
register_ai_derived_purger("search.embeddings_matches", purge_ai_derived, find_ai_derived)
