"""``candidate_search_docs`` maintenance (Epic 5, story 5.1).

``refresh_docs(db, tenant_id, ids)`` is an idempotent upsert from ``candidates.search_port`` projections: the
``doc`` tsvector and ``name_norm`` are computed in SQL (``talent_en`` config, ``f_unaccent``); archived /
merged candidates keep their row with ``is_archived = true``; erased or deleted candidates lose it.
``reindex(db, tenant_id)`` rebuilds every row in batches of 1,000.
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.search import CandidateSearchDoc
from app.schemas.matching_profiles import annualise
from app.services.candidates import search_port as candidates
from app.services.candidates.search_port import SearchProjection
from app.services.matching import settings as matching_settings
from app.services.matching.scoring import convert

CFG = literal_column("'talent_en'::regconfig")
RESUME_MAX = 200_000
BATCH = 1000


def _weighted(text: str | None, weight: str) -> Any:
    return func.setweight(
        func.to_tsvector(CFG, func.coalesce(text or "", "")), literal_column(f"'{weight}'::\"char\"")
    )


def _join(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def _comp_inr(p: SearchProjection, fx: dict[str, Decimal]) -> Decimal | None:
    annual = annualise(p.expected_comp_amount, p.expected_comp_unit)
    if annual is None or not p.expected_comp_currency:
        return None
    value = convert(annual, p.expected_comp_currency, "INR", fx)
    return value.quantize(Decimal("0.01")) if value is not None and value < Decimal("1e12") else None


def _row(tenant_id: uuid.UUID, p: SearchProjection, fx: dict[str, Decimal]) -> dict[str, Any]:
    a_text = _join(p.display_name, p.headline, p.current_title)
    b_text = _join(*p.skill_names)
    c_text = _join(p.current_employer, *p.employers, *p.certifications, *p.tag_names)
    d_text = (p.resume_text or "")[:RESUME_MAX]
    doc = (
        _weighted(a_text, "A")
        .op("||")(_weighted(b_text, "B"))
        .op("||")(_weighted(c_text, "C"))
        .op("||")(_weighted(d_text, "D"))
    )
    return {
        "tenant_id": tenant_id,
        "candidate_id": p.candidate_id,
        "human_id": p.human_id,
        "owner_id": p.owner_id,
        "display_name": p.display_name[:200],
        "name_norm": func.f_unaccent(func.lower(p.display_name)),
        "email": p.email,
        "phone_e164": p.phone_e164,
        "headline": p.headline,
        "current_employer": p.current_employer,
        "current_title": p.current_title,
        "skill_ids": list(p.skill_ids),
        "skill_names": list(p.skill_names),
        "certifications": list(p.certifications),
        "tags": list(p.tags),
        "source": p.source,
        "country": p.country,
        "state": p.state,
        "city": p.city,
        "work_auth": list(p.work_auth),
        "experience_years": p.experience_years,
        "expected_comp_inr": _comp_inr(p, fx),
        "expected_comp_amount": p.expected_comp_amount,
        "expected_comp_currency": p.expected_comp_currency,
        "expected_comp_unit": p.expected_comp_unit,
        "notice_days": p.notice_days,
        "available_from": p.available_from,
        "ai_consent": p.ai_consent,
        "is_archived": p.is_archived,
        "candidate_updated_at": p.candidate_updated_at,
        "resume_text": d_text or None,
        "doc": doc,
        "refreshed_at": func.now(),
    }


async def refresh_docs(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_ids: Sequence[uuid.UUID]
) -> dict[str, int]:
    ids = list(dict.fromkeys(candidate_ids))
    counts = {"upserted": 0, "deleted": 0}
    if not ids:
        return counts
    fx = (await matching_settings.effective(db)).fx
    for start in range(0, len(ids), BATCH):
        chunk = ids[start : start + BATCH]
        projections = await candidates.get_search_projection(db, chunk)
        gone = [cid for cid, p in projections.items() if p is None]
        if gone:
            result = await db.execute(
                delete(CandidateSearchDoc).where(CandidateSearchDoc.candidate_id.in_(gone))
            )
            counts["deleted"] += int(result.rowcount or 0)  # type: ignore[attr-defined]
        for p in projections.values():
            if p is None:
                continue
            values = _row(tenant_id, p, fx)
            stmt = insert(CandidateSearchDoc).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["candidate_id"],
                set_={k: stmt.excluded[k] for k in values if k not in {"candidate_id", "tenant_id"}},
            )
            await db.execute(stmt)
            counts["upserted"] += 1
    return counts


async def delete_docs(db: AsyncSession, candidate_ids: Sequence[uuid.UUID]) -> int:
    if not candidate_ids:
        return 0
    result = await db.execute(
        delete(CandidateSearchDoc).where(CandidateSearchDoc.candidate_id.in_(list(candidate_ids)))
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def reindex(db: AsyncSession, tenant_id: uuid.UUID, *, batch: int = BATCH) -> int:
    """Rebuild every row of the tenant (batches of ``batch``; commits after each batch)."""
    total = 0
    seen: set[uuid.UUID] = set()
    after: uuid.UUID | None = None
    while True:
        ids = await candidates.live_ids(db, after=after, limit=batch)
        if not ids:
            break
        seen.update(ids)
        total += (await refresh_docs(db, tenant_id, ids))["upserted"]
        after = ids[-1]
        await db.commit()
    # Rows of candidates that were erased since (the projection is None -> the row is removed).
    stale = [r[0] for r in await db.execute(select(CandidateSearchDoc.candidate_id)) if r[0] not in seen]
    if stale:
        await refresh_docs(db, tenant_id, stale)
    return total
