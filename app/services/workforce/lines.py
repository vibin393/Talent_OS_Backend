"""Plan lines (Epic 44, FR-222): CRUD on the plan's current revision.

Lines are edited only while the plan is Draft, Budgeted or Revision Draft (409 ``plan_not_editable``). In a
revision, editing an ``unchanged`` copy marks it ``modified`` (and back to ``unchanged`` when it matches the
superseded line again); deleting a copy marks it ``removed`` (an ``added`` line is deleted outright).
``client_id`` comes from the plan (``tos_inherit_org``). Money is masked without ``compensation:view``.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.workforce import HeadcountSlot, WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce import LineIn, LineOut, LineSkillOut, LineUpdate, SlotCounts
from app.services.workforce.common import (
    EDITABLE,
    READ,
    WRITE,
    can_view_money,
    conflict,
    get_plan,
    lines_of,
    mark_picked_up,
    month_key,
    settings_row,
)
from app.services.workforce.validation import raise_errors, validate_line

# Fields compared to decide whether a revision copy differs from the line it supersedes.
COMPARED = (
    "role_title",
    "headcount",
    "line_type",
    "location_text",
    "work_mode",
    "period_start",
    "period_end",
    "skills",
    "budget_amount",
    "bill_rate_amount",
    "rate_unit",
    "project_role_id",
    "notes",
)


def line_status_for(plan: WorkforcePlan) -> str:
    return "forecast" if plan.status == "draft" else "budgeted"


async def slot_counts(db: AsyncSession, lineage_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, SlotCounts]:
    ids = list(set(lineage_ids))
    out: dict[uuid.UUID, SlotCounts] = defaultdict(SlotCounts)
    if not ids:
        return out
    rows = await db.execute(
        select(HeadcountSlot.plan_line_id, HeadcountSlot.status, func.count())
        .where(HeadcountSlot.plan_line_id.in_(ids))
        .group_by(HeadcountSlot.plan_line_id, HeadcountSlot.status)
    )
    for lineage, status, n in rows.tuples():
        setattr(out[lineage], status, int(n))
    return out


def derived_status(line: WorkforcePlanLine, counts: SlotCounts) -> str:
    total = counts.open + counts.requisitioned + counts.filled + counts.cancelled
    if (
        line.status in ("approved", "requisitioned")
        and total
        and counts.open == 0
        and counts.requisitioned == 0
    ):
        return "closed"
    return line.status


def line_out(line: WorkforcePlanLine, counts: SlotCounts | None, *, money: bool) -> LineOut:
    counts = counts or SlotCounts()
    return LineOut(
        id=line.id,
        lineage_id=line.lineage_id or line.id,
        workforce_plan_id=line.workforce_plan_id,
        revision=line.revision,
        role_title=line.role_title,
        skills=[
            LineSkillOut(
                skill_key=s.get("skill_key"),
                name=str(s.get("name") or ""),
                min_proficiency=s.get("min_proficiency"),
            )
            for s in (line.skills or [])
        ],
        headcount=line.headcount,
        line_type=line.line_type,
        location_text=line.location_text,
        work_mode=line.work_mode,
        start_month=month_key(line.period_start),
        period_start=line.period_start,
        period_end=line.period_end,
        budget_amount=line.budget_amount if money else None,
        bill_rate_amount=line.bill_rate_amount if money else None,
        rate_unit=line.rate_unit,
        project_role_id=line.project_role_id,
        status=line.status,
        derived_status=derived_status(line, counts),
        change_kind=line.change_kind,
        supersedes_line_id=line.supersedes_line_id,
        notes=line.notes,
        client_comment=line.client_comment,
        slots=counts,
    )


async def lines_out(ctx: RequestContext, db: AsyncSession, lines: list[WorkforcePlanLine]) -> list[LineOut]:
    counts = await slot_counts(db, [ln.lineage_id or ln.id for ln in lines])
    money = can_view_money(ctx)
    return [line_out(ln, counts.get(ln.lineage_id or ln.id), money=money) for ln in lines]


def _require_editable(plan: WorkforcePlan) -> None:
    if plan.status not in EDITABLE:
        raise conflict(
            "plan_not_editable",
            "Lines can only change while the plan is Draft, Budgeted or a Revision Draft",
            status=plan.status,
        )


async def list_lines(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, revision: int | None = None
) -> list[LineOut]:
    plan = await get_plan(ctx, db, plan_id, permission=READ)
    rows = await lines_of(db, plan.id, plan.current_revision if revision is None else revision)
    return await lines_out(ctx, db, rows)


async def _line(
    db: AsyncSession, plan: WorkforcePlan, line_id: uuid.UUID, *, lock: bool = True
) -> WorkforcePlanLine:
    stmt = select(WorkforcePlanLine).where(
        WorkforcePlanLine.id == line_id, WorkforcePlanLine.workforce_plan_id == plan.id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Plan line not found")
    return row


async def insert_line(
    db: AsyncSession, plan: WorkforcePlan, values: dict[str, Any], **extra: Any
) -> WorkforcePlanLine:
    line_id = uuid.uuid4()
    row = WorkforcePlanLine(
        id=line_id,
        tenant_id=plan.tenant_id,
        workforce_plan_id=plan.id,
        client_id=plan.client_id,
        revision=plan.current_revision,
        lineage_id=line_id,
        status=line_status_for(plan),
        change_kind="added",
        **values,
        **extra,
    )
    db.add(row)
    await db.flush()
    return row


async def create_line(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, data: LineIn) -> LineOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    _require_editable(plan)
    settings = await settings_row(db)
    values, errors = await validate_line(
        ctx, db, plan, data.model_dump(), max_headcount=settings.max_headcount_per_line
    )
    raise_errors(errors)
    row = await insert_line(db, plan, values)
    mark_picked_up(ctx, plan)
    await db.flush()
    return (await lines_out(ctx, db, [row]))[0]


def _differs(a: WorkforcePlanLine, b: WorkforcePlanLine) -> bool:
    return any(getattr(a, f) != getattr(b, f) for f in COMPARED)


async def update_line(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, line_id: uuid.UUID, data: LineUpdate
) -> LineOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    _require_editable(plan)
    row = await _line(db, plan, line_id)
    if row.revision != plan.current_revision:
        raise conflict("line_not_current", "Only lines of the plan's current revision can be edited")
    if row.change_kind == "removed":
        raise conflict("line_removed", "This line is removed in the revision")
    settings = await settings_row(db)
    sent = data.model_dump(exclude_unset=True)
    sent["_current_start"] = row.period_start
    values, errors = await validate_line(
        ctx, db, plan, sent, max_headcount=settings.max_headcount_per_line, partial=True
    )
    if "headcount" in sent and sent["headcount"] is None:
        errors.append(("headcount", "Headcount must be at least 1"))
    if "role_title" in sent and not sent["role_title"]:
        errors.append(("role_title", "Role is required"))
    raise_errors(errors)
    for key, value in values.items():
        setattr(row, key, value)
    if row.period_end < row.period_start:
        row.period_end = plan.period_end
    if plan.status == "revision_draft" and row.supersedes_line_id is not None:
        previous = await db.get(WorkforcePlanLine, row.supersedes_line_id)
        if previous is not None:
            row.change_kind = "modified" if _differs(row, previous) else "unchanged"
    mark_picked_up(ctx, plan)
    await db.flush()
    return (await lines_out(ctx, db, [row]))[0]


async def delete_line(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, line_id: uuid.UUID) -> None:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    _require_editable(plan)
    row = await _line(db, plan, line_id)
    if row.revision != plan.current_revision:
        raise conflict("line_not_current", "Only lines of the plan's current revision can be removed")
    if row.supersedes_line_id is not None:
        row.change_kind = "removed"
    else:
        await db.delete(row)
    mark_picked_up(ctx, plan)
    await db.flush()
