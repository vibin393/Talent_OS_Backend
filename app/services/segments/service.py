# ruff: file-ignore[non-imperative-mood]
"""Segments contract (Epic 38, filling the Epic 36 stub, AD-39). The signature is FINAL
(``tests/contracts/signatures.json``).

Evaluation always runs through ``core.semantic`` (AD-42): a request context evaluates with the **caller's**
scope, field permissions and RLS (purpose ``segment``); a context without a principal (tasks) evaluates with
``SemanticContext.for_system(..., "refresh")`` -- tenant-wide, RLS still on. Readers of a system-evaluated
membership re-apply their own scope (``pools_service.member_ids``).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.semantic import (
    Definition,
    SemanticContext,
    SemanticResult,
    compile,
    count,
    execute,
    get_source,
    ids,
    validate,
)
from app.core.skill_refs import SkillKey
from app.models.talent_pools import Segment
from app.schemas.segments import SegmentEvaluation
from app.services.segments import definitions

__all__ = ["evaluate", "preview_result", "reason_for", "semantic_context"]

MAX_PREVIEW = 500


def semantic_context(ctx: RequestContext, db: AsyncSession, purpose: str = "segment") -> SemanticContext:
    """Caller-scoped semantic context, or the system one when ``ctx`` carries no principal (tasks)."""
    if ctx.principal is None:
        if ctx.tenant_id is None:  # pragma: no cover - tasks always set the tenant
            raise NotFound("Segment not found")
        return SemanticContext.for_system(ctx.tenant_id, db, "refresh")
    return SemanticContext(ctx, db, purpose)  # type: ignore[arg-type]


async def preview_result(sctx: SemanticContext, defn: Definition, limit: int | None) -> SemanticResult:
    """First ``limit`` (<= 500) rows with the source's preview columns, plus the total."""
    cap = MAX_PREVIEW if limit is None else max(1, min(int(limit), MAX_PREVIEW))
    fields = [
        f for f in definitions.PREVIEW_FIELDS.get(defn.source, ()) if f in get_source(defn.source).fields
    ]
    preview = defn.model_copy(update={"fields": fields})
    return await execute(await compile(preview, sctx), sctx, limit=cap)


async def _skill_reasons(ctx: RequestContext, db: AsyncSession, defn: Definition) -> list[dict[str, str]]:
    """One ``skills_service.expand`` call per definition (segments sit above skills, section 0.9)."""
    keys = definitions.expanding_skill_refs(defn)
    if not keys:
        return []
    from app.services import skills_service

    refs: list[SkillKey] = []
    for k in keys:
        try:
            refs.append(SkillKey.parse(k))
        except ValueError:
            continue
    if not refs:
        return []
    expanded = await skills_service.expand(ctx, db, refs, depth=1)
    out: dict[str, str] = {}
    for e in expanded:
        key = e.key.as_str()
        weight = str(Decimal(e.weight).quantize(Decimal("0.01")))
        if key not in out or Decimal(out[key]) < Decimal(weight):
            out[key] = weight
    return [{"skill_key": k, "weight": w} for k, w in out.items()]


async def reason_for(
    ctx: RequestContext, db: AsyncSession, defn: Definition, segment_id: uuid.UUID | None, version: int
) -> dict[str, Any]:
    """The shared per-definition member reason (spec A3: per-leaf truth is not computed in Phase 3)."""
    reason: dict[str, Any] = {
        "segment_id": str(segment_id) if segment_id else None,
        "version": version,
        "matched": definitions.matched_labels(defn),
    }
    skills = await _skill_reasons(ctx, db, defn)
    if skills:
        reason["skills"] = skills
    return reason


async def evaluate(
    ctx: RequestContext,
    db: AsyncSession,
    segment_id: uuid.UUID | None = None,
    *,
    definition: Definition | None = None,
    mode: Literal["ids", "count", "preview"] = "ids",
    limit: int | None = None,
    with_reasons: bool = False,
) -> SegmentEvaluation:
    """Evaluate a saved segment or an ad-hoc ``definition`` (exactly one; 422 ``segment_or_definition``)."""
    if (segment_id is None) == (definition is None):
        raise ValidationFailed(
            "Pass exactly one of segment_id or definition",
            code="segment_or_definition",
            details={"path": "segment_id"},
        )
    version = 1
    if segment_id is not None:
        seg = (await db.execute(select(Segment).where(Segment.id == segment_id))).scalar_one_or_none()
        if seg is None:
            raise NotFound("Segment not found")
        defn = validate(seg.definition)
        version = seg.definition_version
    else:
        assert definition is not None
        defn = validate(definition)
    sctx = semantic_context(ctx, db)
    subject_ids: tuple[uuid.UUID, ...] = ()
    if mode == "count":
        total = await count(defn, sctx)
    elif mode == "preview":
        result = await preview_result(sctx, defn, limit)
        subject_ids = tuple(r["subject_id"] for r in result.rows)
        total = int(result.total or 0)
    else:
        found = await ids(defn, sctx, limit=limit)
        subject_ids = tuple(found)
        total = len(found)
    reasons: dict[uuid.UUID, dict[str, Any]] | None = None
    if with_reasons and mode != "count":
        shared = await reason_for(ctx, db, defn, segment_id, version)
        reasons = dict.fromkeys(subject_ids, shared)
    return SegmentEvaluation(
        segment_id=segment_id,
        source=defn.source,
        definition_version=version,
        count=total,
        subject_ids=subject_ids,
        reasons=reasons,
        evaluated_at=datetime.now(UTC),
    )
