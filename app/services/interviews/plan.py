"""Per-demand interview stage plans (7.1, FR-39).

* ``get_plan`` -- on the first read of a demand without stages it seeds from the legacy
  ``demand.interview_plan`` jsonb when present, otherwise from the tenant default template.
* ``apply_template`` -- only while no interview exists on the plan (409 ``plan_has_interviews``).
* ``save_plan`` -- replaces the ordered list: removing a stage with a ``scheduled`` interview is 409
  ``stage_has_scheduled_interviews`` (``details.interview_ids``); stages with only completed / cancelled
  interviews are archived (never deleted) so their interviews and scorecards keep ``stage_id``; renaming /
  reordering never touches interviews (they snapshot ``stage_name``); ``is_final`` is recomputed.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import check
from app.models.interviews import STAGE_KINDS, Interview, InterviewKit, InterviewStage
from app.schemas.interviews import ApplyTemplateIn, PlanOut, PlanSave, PlanStageOut
from app.services.interviews import configuration, ports
from app.services.interviews.common import SCHEDULE, conflict, now, principal_of, settings_row, tenant_of

_TEMP_OFFSET = 100_000  # positions are moved out of the way first (unique (demand_id, position) index)

# Epic 40 (additive): stage-plan validators and scheduling guards registered by other domains (the interviews
# domain imports no Phase 3 package). A validator gets the requested stages and raises a DomainError; a guard gets
# the stage an interview is being scheduled in and raises to refuse it (e.g. 409 ``stage_is_assessment``).
PlanValidator = Callable[[RequestContext, AsyncSession, list[Any]], Awaitable[None]]
ScheduleGuard = Callable[[AsyncSession, InterviewStage], Awaitable[None]]
_plan_validators: list[PlanValidator] = []
_schedule_guards: list[ScheduleGuard] = []


def register_stage_plan_validator(fn: PlanValidator) -> None:
    if fn not in _plan_validators:
        _plan_validators.append(fn)


def register_schedule_guard(fn: ScheduleGuard) -> None:
    if fn not in _schedule_guards:
        _schedule_guards.append(fn)


async def run_schedule_guards(db: AsyncSession, stage: InterviewStage) -> None:
    for fn in _schedule_guards:
        await fn(db, stage)


async def active_stages(db: AsyncSession, demand_id: uuid.UUID) -> list[InterviewStage]:
    rows = await db.execute(
        select(InterviewStage)
        .where(InterviewStage.demand_id == demand_id, InterviewStage.archived_at.is_(None))
        .order_by(InterviewStage.position)
    )
    return list(rows.scalars())


async def _interview_counts(db: AsyncSession, stage_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    if not stage_ids:
        return {}
    rows = await db.execute(
        select(Interview.stage_id, Interview.status, func.count())
        .where(Interview.stage_id.in_(stage_ids))
        .group_by(Interview.stage_id, Interview.status)
    )
    out: dict[uuid.UUID, dict[str, int]] = {}
    for stage_id, status, count in rows.all():
        out.setdefault(stage_id, {})[status] = int(count)
    return out


async def stage_outs(db: AsyncSession, stages: list[InterviewStage]) -> list[PlanStageOut]:
    kit_ids = {s.kit_id for s in stages if s.kit_id}
    names: dict[uuid.UUID, str] = {}
    if kit_ids:
        names = dict(
            (await db.execute(select(InterviewKit.id, InterviewKit.name).where(InterviewKit.id.in_(kit_ids))))
            .tuples()
            .all()
        )
    counts = await _interview_counts(db, [s.id for s in stages])
    return [
        PlanStageOut(
            id=s.id,
            position=s.position,
            name=s.name,
            kind=s.kind,
            kit_id=s.kit_id,
            kit_name=names.get(s.kit_id) if s.kit_id else None,
            default_duration_minutes=s.default_duration_minutes,
            is_final=s.is_final,
            interview_counts=counts.get(s.id, {}),
            assessment_id=s.assessment_id,
        )
        for s in stages
    ]


async def _has_interviews(db: AsyncSession, demand_id: uuid.UUID) -> bool:
    return (
        await db.execute(select(Interview.id).where(Interview.demand_id == demand_id).limit(1))
    ).first() is not None


def _mark_final(stages: list[InterviewStage]) -> None:
    for i, s in enumerate(stages):
        s.is_final = i == len(stages) - 1


async def _insert_stages(
    db: AsyncSession, demand_id: uuid.UUID, specs: list[dict[str, Any]]
) -> list[InterviewStage]:
    tenant_id = tenant_of(db)
    default_duration = (await settings_row(db)).default_duration_minutes
    kit_ids = {uuid.UUID(str(s["kit_id"])) for s in specs if s.get("kit_id")}
    live_kits = await configuration.active_kit_ids(db, kit_ids)
    rows: list[InterviewStage] = []
    for i, spec in enumerate(specs):
        kind = str(spec.get("kind") or "other")
        kit = uuid.UUID(str(spec["kit_id"])) if spec.get("kit_id") else None
        duration = int(spec.get("default_duration_minutes") or default_duration)
        row = InterviewStage(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            demand_id=demand_id,
            position=i,
            name=str(spec.get("name") or f"Stage {i + 1}")[:200],
            kind=kind if kind in STAGE_KINDS else "other",
            kit_id=kit if kit in live_kits else None,
            default_duration_minutes=min(max(duration, 15), 480),
        )
        rows.append(row)
        db.add(row)
    _mark_final(rows)
    await db.flush()
    return rows


async def ensure_plan(db: AsyncSession, demand: Any) -> list[InterviewStage]:
    """Stages of ``demand``; seeds them on first use (legacy jsonb, else the tenant default template)."""
    stages = await active_stages(db, demand.id)
    if stages:
        return stages
    any_row = (
        await db.execute(select(InterviewStage.id).where(InterviewStage.demand_id == demand.id).limit(1))
    ).first()
    if any_row is not None:  # a plan emptied on purpose stays empty
        return []
    legacy = [s for s in (demand.interview_plan or []) if isinstance(s, dict) and s.get("name")]
    if legacy:
        return await _insert_stages(db, demand.id, legacy)
    template = await configuration.default_template(db)
    if template is None:
        return []
    return await _insert_stages(db, demand.id, list(template.stages or []))


async def _plan_out(db: AsyncSession, demand_id: uuid.UUID, stages: list[InterviewStage]) -> PlanOut:
    return PlanOut(
        demand_id=demand_id,
        stages=await stage_outs(db, stages),
        has_interviews=await _has_interviews(db, demand_id),
    )


async def get_plan(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> PlanOut:
    principal_of(ctx)
    demand = await ports.demand_visible(ctx, db, demand_id)
    stages = await ensure_plan(db, demand)
    return await _plan_out(db, demand.id, stages)


async def _visible_for_schedule(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Any:
    principal = principal_of(ctx)
    demand = await ports.demand_visible(ctx, db, demand_id)
    check(principal, SCHEDULE)
    return demand


async def apply_template(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: ApplyTemplateIn
) -> PlanOut:
    demand = await _visible_for_schedule(ctx, db, demand_id)
    template = await configuration.get_template_row(db, data.template_id)
    if template.archived_at is not None:
        raise NotFound("Stage template not found")
    if await _has_interviews(db, demand.id):
        raise conflict(
            "plan_has_interviews", "A template can only be applied before any interview is scheduled"
        )
    existing = (
        await db.execute(select(InterviewStage).where(InterviewStage.demand_id == demand.id))
    ).scalars()
    for row in existing:
        await db.delete(row)
    await db.flush()
    stages = await _insert_stages(db, demand.id, list(template.stages or []))
    return await _plan_out(db, demand.id, stages)


async def save_plan(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: PlanSave) -> PlanOut:
    demand = await _visible_for_schedule(ctx, db, demand_id)
    current = await ensure_plan(db, demand)
    by_id = {s.id: s for s in current}
    wanted_ids = [s.id for s in data.stages if s.id is not None]
    if len(set(wanted_ids)) != len(wanted_ids):
        raise ValidationFailed("A stage appears twice", code="duplicate_stage")
    unknown = [str(i) for i in wanted_ids if i not in by_id]
    if unknown:
        raise NotFound("Stage not found", details={"stage_ids": unknown})
    kit_ids = {s.kit_id for s in data.stages if s.kit_id}
    missing = kit_ids - await configuration.active_kit_ids(db, kit_ids)
    if missing:
        raise ValidationFailed(
            "Unknown or archived interview kit",
            code="invalid_kit",
            details={"kit_ids": sorted(map(str, missing))},
        )

    for validator in _plan_validators:
        await validator(ctx, db, list(data.stages))
    removed = [s for s in current if s.id not in set(wanted_ids)]
    if removed:
        scheduled = await db.execute(
            select(Interview.id)
            .where(Interview.stage_id.in_([s.id for s in removed]), Interview.status == "scheduled")
            .order_by(Interview.starts_at)
        )
        ids = [str(i) for i in scheduled.scalars()]
        if ids:
            raise conflict(
                "stage_has_scheduled_interviews",
                "A stage you removed still has scheduled interviews",
                interview_ids=ids,
            )
    # Move every live stage out of the unique position range first, then write the final order.
    for s in current:
        s.position = s.position + _TEMP_OFFSET
    await db.flush()
    held = (await _interview_counts(db, [s.id for s in removed])) if removed else {}
    at = now()
    for s in removed:
        if held.get(s.id):
            s.archived_at = at  # completed / cancelled interviews keep pointing at it
            s.is_final = False
        else:
            await db.delete(s)
    await db.flush()
    tenant_id = tenant_of(db)
    ordered: list[InterviewStage] = []
    for i, spec in enumerate(data.stages):
        row = by_id.get(spec.id) if spec.id is not None else None
        if row is None:
            row = InterviewStage(id=uuid.uuid4(), tenant_id=tenant_id, demand_id=demand.id, position=i)
            db.add(row)
        row.position = i
        row.name = spec.name
        row.kind = spec.kind
        row.kit_id = spec.kit_id
        row.default_duration_minutes = spec.default_duration_minutes
        row.assessment_id = spec.assessment_id if spec.kind == "assessment" else None  # Epic 40
        ordered.append(row)
    _mark_final(ordered)
    await db.flush()
    return await _plan_out(db, demand.id, ordered)


async def stage_for_demand(db: AsyncSession, demand_id: uuid.UUID, stage_id: uuid.UUID) -> InterviewStage:
    """Return a live stage of the demand's plan (409 ``stage_not_in_plan`` otherwise)."""
    row = await db.get(InterviewStage, stage_id)
    if row is None or row.demand_id != demand_id or row.archived_at is not None:
        raise conflict("stage_not_in_plan", "The interview stage does not belong to this demand's plan")
    return row
