# ruff: noqa: E501, D401
"""Skill, new-skill, contract and rate-card suggestions (Epic 55, stories 55.2 / 55.3, FR-282).

Agent tools only **write suggestions**: they never change ``demand_skills`` and never create ``TenantSkill`` rows
(skills are resolved with ``resolve_skill(..., create_pending=False)``). A person accepts a suggestion (``demand:update``):
skills go through the P1 ``set_skills``, where the person's own action may create a pending tenant skill (P1
semantics). Contract / rate-card suggestions record the link on the suggestion (the P1 ``demands`` table has no
contract column and is not altered).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.models.demand_insights import DemandSuggestion
from app.models.demands import Demand, DemandSkill
from app.schemas.demand_insights import AcceptSuggestionIn, SuggestionOut, SuggestSkillItem


def _now() -> datetime:
    return datetime.now(UTC)


def norm_label(label: str) -> str:
    return " ".join(label.lower().split())


async def resolve_ref(ctx: RequestContext, db: AsyncSession, label: str, skill_ref_kind: str | None = None,
                      skill_id: uuid.UUID | None = None) -> Any:  # fmt: skip
    """A catalogue ``SkillRef`` for the label (exact / alias / slug) or the given ref -- **never creates**."""
    from app.schemas.candidates import SkillRefIn
    from app.services.candidates import skills as catalog

    if skill_id is not None and skill_ref_kind is not None:
        ref = await catalog.get_ref(db, SkillRefIn(kind=skill_ref_kind, id=skill_id))  # type: ignore[arg-type]
        if ref is not None:
            return ref
    return await catalog.resolve_skill(ctx, db, label, create_pending=False)


async def _existing_skill_keys(db: AsyncSession, demand_id: uuid.UUID) -> set[str]:
    rows = (await db.execute(select(DemandSkill).where(DemandSkill.demand_id == demand_id))).scalars()
    keys: set[str] = set()
    for r in rows:
        keys.add(f"label:{norm_label(r.skill_label)}")
        if r.skill_id and r.skill_ref_kind:
            keys.add(f"{r.skill_ref_kind}:{r.skill_id}")
    return keys


async def _insert(db: AsyncSession, values: dict[str, Any]) -> uuid.UUID | None:
    stmt = (
        pg_insert(DemandSuggestion)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "demand_id", "dedupe_key"], index_where=text("status = 'pending'")
        )
        .returning(DemandSuggestion.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def add_skill_suggestions(
    ctx: RequestContext,
    db: AsyncSession,
    demand: Demand,
    items: list[SuggestSkillItem],
    *,
    run_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    have = await _existing_skill_keys(db, demand.id)
    ids: list[uuid.UUID] = []
    for item in items:
        ref = await resolve_ref(ctx, db, item.label, item.skill_ref_kind, item.skill_id)
        label_key = f"label:{norm_label(item.label)}"
        if label_key in have or (ref is not None and f"{ref.kind}:{ref.id}" in have):
            continue
        kind = "skill" if ref is not None else "new_skill"
        payload = {
            "label": ref.name if ref is not None else " ".join(item.label.split())[:100],
            "skill_ref": {"kind": ref.kind, "id": str(ref.id), "status": ref.status}
            if ref is not None
            else None,
            "skill_kind": item.skill_kind,
            "min_proficiency": item.min_proficiency,
            "min_years": str(item.min_years) if item.min_years is not None else None,
            "evidence": (item.evidence or "")[:300] or None,
        }
        new_id = await _insert(
            db,
            {
                "id": uuid.uuid4(), "tenant_id": demand.tenant_id, "demand_id": demand.id, "kind": kind,
                "payload": payload, "status": "pending", "run_id": run_id,
                "dedupe_key": f"skill:{norm_label(item.label)}"[:200],
            },
        )  # fmt: skip
        if new_id is not None:
            ids.append(new_id)
    return ids


async def add_link_suggestion(
    db: AsyncSession, demand: Demand, kind: str, target_id: uuid.UUID, label: str, *, run_id: uuid.UUID | None
) -> uuid.UUID | None:
    return await _insert(
        db,
        {
            "id": uuid.uuid4(), "tenant_id": demand.tenant_id, "demand_id": demand.id, "kind": kind,
            "payload": {"id": str(target_id), "label": label[:200]}, "status": "pending", "run_id": run_id,
            "dedupe_key": f"{kind}:{target_id}",
        },
    )  # fmt: skip


def out(row: DemandSuggestion) -> SuggestionOut:
    return SuggestionOut(
        id=row.id, kind=row.kind, payload=row.payload, status=row.status, created_at=row.created_at,  # type: ignore[arg-type]
        decided_at=row.decided_at,
    )  # fmt: skip


async def for_demand(db: AsyncSession, demand_id: uuid.UUID) -> list[SuggestionOut]:
    rows = (
        await db.execute(
            select(DemandSuggestion)
            .where(
                DemandSuggestion.demand_id == demand_id, DemandSuggestion.status.in_(("pending", "accepted"))
            )
            .order_by(DemandSuggestion.created_at, DemandSuggestion.id)
        )
    ).scalars()
    return [out(r) for r in rows]


async def pending_count(db: AsyncSession, demand_id: uuid.UUID) -> int:
    from sqlalchemy import func

    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(DemandSuggestion)
                .where(DemandSuggestion.demand_id == demand_id, DemandSuggestion.status == "pending")
            )
        ).scalar_one()
    )


async def _pending(db: AsyncSession, suggestion_id: uuid.UUID) -> DemandSuggestion:
    row = (
        await db.execute(
            select(DemandSuggestion).where(DemandSuggestion.id == suggestion_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Suggestion not found")
    if row.status != "pending":
        raise ConflictError("This suggestion was already decided", code="suggestion_not_pending")
    return row


async def accept(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    data: AcceptSuggestionIn,
) -> None:
    from app.schemas.demands import DemandSkillIn
    from app.services.demands.common import UPDATE, acting_as, get_for
    from app.services.demands.records import set_skills

    require_human(actor_from_ctx(ctx))
    demand = await get_for(ctx, db, demand_id, UPDATE)
    row = await _pending(db, suggestion_id)
    if row.demand_id != demand.id:
        raise NotFound("Suggestion not found")
    if row.kind in ("skill", "new_skill"):
        payload = row.payload
        current = list(
            (
                await db.execute(
                    select(DemandSkill)
                    .where(DemandSkill.demand_id == demand.id)
                    .order_by(DemandSkill.position)
                )
            ).scalars()
        )
        items = [
            DemandSkillIn(
                label=s.skill_label,
                skill_id=s.skill_id,
                skill_ref_kind=s.skill_ref_kind,
                kind=s.kind,  # type: ignore[arg-type]
                min_years=s.min_years,
                min_proficiency=s.min_proficiency,
                expand=s.expand,
            )
            for s in current
        ]
        ref = payload.get("skill_ref") or {}
        items.append(
            DemandSkillIn(
                label=str(payload.get("label"))[:200],
                skill_id=uuid.UUID(ref["id"]) if ref.get("id") else None,
                skill_ref_kind=ref.get("kind"),
                kind=data.skill_kind or payload.get("skill_kind") or "mandatory",
                min_years=Decimal(payload["min_years"]) if payload.get("min_years") else None,
                min_proficiency=data.min_proficiency
                if data.min_proficiency is not None
                else payload.get("min_proficiency"),
            )
        )
        with acting_as(db, "user", getattr(ctx.principal, "id", None)):
            await set_skills(ctx, db, demand, items)
        if "mandatory_skills" in (demand.ai_extracted_fields or []) or "preferred_skills" in (
            demand.ai_extracted_fields or []
        ):
            pass  # accepting a suggestion does not confirm the AI-extracted skills (P1 confirm does)
    row.status = "accepted"
    row.decided_by = getattr(ctx.principal, "id", None)
    row.decided_at = _now()
    await db.flush()


async def dismiss(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, suggestion_id: uuid.UUID
) -> None:
    from app.services.demands.common import UPDATE, get_for

    require_human(actor_from_ctx(ctx))
    demand = await get_for(ctx, db, demand_id, UPDATE)
    row = await _pending(db, suggestion_id)
    if row.demand_id != demand.id:
        raise NotFound("Suggestion not found")
    row.status = "dismissed"
    row.decided_by = getattr(ctx.principal, "id", None)
    row.decided_at = _now()
    await db.flush()


__all__ = [
    "accept",
    "add_link_suggestion",
    "add_skill_suggestions",
    "dismiss",
    "for_demand",
    "norm_label",
    "out",
    "pending_count",
    "resolve_ref",
]
