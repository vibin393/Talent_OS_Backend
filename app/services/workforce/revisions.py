# ruff: noqa: E501
"""Plan revisions (Epic 44, FR-223, PRD 5.6).

``approved -> revision_draft`` copies the effective lines into revision ``r`` (``change_kind='unchanged'``,
``supersedes_line_id`` = the copied row, same ``lineage_id``); edits mark lines ``modified`` / ``added`` /
``removed``. The approver sees only changed lines (``diff``). While a revision is drafted or pending, the approved
revision stays effective (slots, conversion and actuals keep working). ``revision_draft -> approved`` discards
the revision (its lines are deleted).
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.workforce import WorkforcePlan, WorkforcePlanLine, WorkforcePlanRevision
from app.schemas.workforce import DiffLine, DiffOut, PlanOut, RevisionOut
from app.services.workforce.common import (
    READ,
    WRITE,
    can_view_money,
    get_plan,
    lines_of,
    month_key,
    now,
    user_id,
)
from app.services.workforce.lines import COMPARED
from app.services.workforce.plans import invalid_transition, plan_out

MONEY_FIELDS = ("budget_amount", "bill_rate_amount")


def snapshot(line: WorkforcePlanLine, *, money: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in COMPARED:
        value = getattr(line, f)
        if f in MONEY_FIELDS:
            value = (str(value) if value is not None else None) if money else None
        elif f == "period_start":
            value = month_key(value)
        elif f == "period_end" or isinstance(value, uuid.UUID):
            value = str(value) if value is not None else None
        out[f] = value
    return out


async def revision_row(
    db: AsyncSession, plan_id: uuid.UUID, revision_no: int
) -> WorkforcePlanRevision | None:
    return (
        await db.execute(
            select(WorkforcePlanRevision).where(
                WorkforcePlanRevision.workforce_plan_id == plan_id,
                WorkforcePlanRevision.revision_no == revision_no,
            )
        )
    ).scalar_one_or_none()


async def ensure_revision_row(
    ctx: RequestContext, db: AsyncSession, plan: WorkforcePlan, reason: str | None = None
) -> WorkforcePlanRevision:
    row = await revision_row(db, plan.id, plan.current_revision)
    if row is None:
        row = WorkforcePlanRevision(
            id=uuid.uuid4(),
            tenant_id=plan.tenant_id,
            workforce_plan_id=plan.id,
            revision_no=plan.current_revision,
            status="draft",
            reason=reason if plan.current_revision else (reason or "Initial plan"),
            created_by=user_id(ctx),
        )
        db.add(row)
        await db.flush()
    return row


async def diff_lines(
    db: AsyncSession, plan_id: uuid.UUID, revision: int, *, money: bool = True
) -> list[DiffLine]:
    rows = await lines_of(db, plan_id, revision)
    previous_ids = [ln.supersedes_line_id for ln in rows if ln.supersedes_line_id]
    previous = (
        {
            p.id: p
            for p in (
                await db.execute(select(WorkforcePlanLine).where(WorkforcePlanLine.id.in_(previous_ids)))
            ).scalars()
        }
        if previous_ids
        else {}
    )
    out: list[DiffLine] = []
    for ln in rows:
        if ln.change_kind == "unchanged":
            continue
        before_row = previous.get(ln.supersedes_line_id) if ln.supersedes_line_id else None
        before = snapshot(before_row, money=money) if before_row is not None else None
        after = None if ln.change_kind == "removed" else snapshot(ln, money=money)
        if before is not None and after is not None:
            changed = [f for f in COMPARED if before.get(f) != after.get(f)]
        else:
            changed = list(COMPARED)
        out.append(
            DiffLine(
                lineage_id=ln.lineage_id or ln.id,
                line_id=ln.id,
                change_kind=ln.change_kind,  # type: ignore[arg-type]
                role_title=ln.role_title,
                before=before,
                after=after,
                changed_fields=changed,
            )
        )
    return out


# ================================================================ transitions
async def start_revision(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, reason: str | None
) -> PlanOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status != "approved" or plan.approved_revision is None:
        raise invalid_transition(plan, "revision_draft")
    last = (
        await db.execute(
            select(func.max(WorkforcePlanRevision.revision_no)).where(
                WorkforcePlanRevision.workforce_plan_id == plan.id
            )
        )
    ).scalar_one()
    new_no = max(int(last or 0), plan.current_revision, plan.approved_revision) + 1
    effective = await lines_of(db, plan.id, plan.approved_revision, include_removed=False)
    plan.current_revision = new_no
    plan.status = "revision_draft"
    for ln in effective:
        db.add(
            WorkforcePlanLine(
                id=uuid.uuid4(),
                tenant_id=plan.tenant_id,
                workforce_plan_id=plan.id,
                client_id=plan.client_id,
                revision=new_no,
                lineage_id=ln.lineage_id or ln.id,
                supersedes_line_id=ln.id,
                change_kind="unchanged",
                status=ln.status,
                role_title=ln.role_title,
                period_start=ln.period_start,
                period_end=ln.period_end,
                headcount=ln.headcount,
                project_role_id=ln.project_role_id,
                skills=list(ln.skills or []),
                line_type=ln.line_type,
                location_text=ln.location_text,
                work_mode=ln.work_mode,
                budget_amount=ln.budget_amount,
                bill_rate_amount=ln.bill_rate_amount,
                rate_unit=ln.rate_unit,
                notes=ln.notes,
                client_comment=ln.client_comment,
            )
        )
    await db.flush()
    await ensure_revision_row(ctx, db, plan, reason)
    audit.record("workforce.revision_started", "workforce_plan", plan.id, session=db, revision=new_no)
    return await plan_out(ctx, db, plan)


async def discard_revision(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> PlanOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status != "revision_draft" or plan.approved_revision is None:
        raise invalid_transition(plan, "approved")
    discarded = plan.current_revision
    row = await revision_row(db, plan.id, discarded)
    if row is not None:  # keep what was proposed: the lines themselves are deleted
        row.diff = [d.model_dump(mode="json") for d in await diff_lines(db, plan.id, discarded)]
    for ln in await lines_of(db, plan.id, discarded):
        await db.delete(ln)
    if row is not None:
        row.status = "discarded"
        row.decided_at = now()
    plan.current_revision = plan.approved_revision
    plan.status = "approved"
    await db.flush()
    audit.record("workforce.revision_discarded", "workforce_plan", plan.id, session=db, revision=discarded)
    return await plan_out(ctx, db, plan)


# ================================================================ reads
async def list_revisions(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> list[RevisionOut]:
    plan = await get_plan(ctx, db, plan_id, permission=READ)
    rows = list(
        (
            await db.execute(
                select(WorkforcePlanRevision)
                .where(WorkforcePlanRevision.workforce_plan_id == plan.id)
                .order_by(WorkforcePlanRevision.revision_no.desc())
            )
        ).scalars()
    )
    counts = dict(
        (
            await db.execute(
                select(WorkforcePlanLine.revision, func.count())
                .where(
                    WorkforcePlanLine.workforce_plan_id == plan.id,
                    WorkforcePlanLine.change_kind != "unchanged",
                )
                .group_by(WorkforcePlanLine.revision)
            )
        )
        .tuples()
        .all()
    )
    return [
        RevisionOut(
            id=r.id,
            revision_no=r.revision_no,
            status=r.status,
            reason=r.reason,
            created_by=r.created_by,
            approval_request_id=r.approval_request_id,
            submitted_at=r.submitted_at,
            decided_at=r.decided_at,
            created_at=r.created_at,
            changed_lines=int(counts.get(r.revision_no, 0)) if r.status != "discarded" else len(r.diff or []),
        )
        for r in rows
    ]


async def get_diff(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, revision_no: int) -> DiffOut:
    plan = await get_plan(ctx, db, plan_id, permission=READ)
    row = await revision_row(db, plan.id, revision_no)
    if row is None and revision_no != plan.current_revision:
        raise NotFound("Revision not found")
    money = can_view_money(ctx)
    if row is not None and row.status == "discarded":
        lines = [DiffLine(**d) for d in (row.diff or [])]
        if not money:
            for d in lines:
                for side in (d.before, d.after):
                    if side:
                        for f in MONEY_FIELDS:
                            side[f] = None
    else:
        lines = await diff_lines(db, plan.id, revision_no, money=money)
    return DiffOut(
        revision_no=revision_no, status=row.status if row else "draft", money_masked=not money, lines=lines
    )


def total_budget(lines: list[WorkforcePlanLine]) -> Decimal:
    return sum((ln.budget_amount or Decimal(0) for ln in lines if ln.change_kind != "removed"), Decimal(0))
