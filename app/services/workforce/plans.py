# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Workforce plans (Epic 44, FR-222/223): CRUD, copy and the simple transitions (AD-4).

Transitions here: ``draft -> budgeted`` (every line has a budget; Client-forecast skill names must match the
catalogue), ``budgeted -> draft``, ``rejected -> draft``. Submit / approval live in ``approval.py``, revisions in
``revisions.py``. Anything else is 409 ``invalid_transition``. Plans are data-scoped on owner / BU
(``workforce_plan:read|write`` scope) and 404 outside it.
"""

import calendar
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.fiscal import fiscal_year
from app.models.workforce import HeadcountSlot, WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce import PlanCopyIn, PlanCreate, PlanOut, PlanUpdate, SlotCounts
from app.services.workforce.common import (
    EDITABLE,
    READ,
    WRITE,
    add_months,
    can_view_money,
    conflict,
    get_plan,
    invalid,
    lines_of,
    mark_picked_up,
    months_between,
    scoped,
    system_ctx,
    tenant_currency,
    tenant_of,
    user_id,
)
from app.services.workforce.validation import resolve_skills

MAX_MONTHS = 24


def fiscal_label(start: date, end: date) -> str:
    a, b = fiscal_year(start), fiscal_year(end)
    return f"FY {a}" if a == b else f"FY {a} to {b}"  # NOSONAR


def _check_period(start: date, end: date) -> None:
    if end <= start:
        raise invalid("period_invalid", "The plan must end after it starts", field="period_end")
    if end >= add_months(start, MAX_MONTHS):
        raise invalid("period_too_long", "A plan covers at most 24 months", field="period_end")


async def _check_refs(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    client_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    business_unit_id: uuid.UUID | None,
) -> None:
    from app.models.clients import Client
    from app.models.identity import BusinessUnit

    if client_id is not None and await db.get(Client, client_id) is None:
        raise invalid("unknown_client", "Unknown client", field="client_id")
    if business_unit_id is not None and await db.get(BusinessUnit, business_unit_id) is None:
        raise invalid("unknown_business_unit", "Unknown business unit", field="business_unit_id")
    if project_id is not None:
        from app.services import projects_service

        try:
            context = await projects_service.project_context(system_ctx(tenant_of(db)), db, project_id)
        except NotFound:
            raise invalid("unknown_project", "Unknown project", field="project_id") from None
        if client_id is not None and context.client_id != client_id:
            raise invalid(
                "project_client_mismatch", "The project belongs to another client", field="project_id"
            )


# ================================================================ output
async def _names(
    db: AsyncSession, plans: list[WorkforcePlan]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    from app.models.clients import Client
    from app.models.identity import User

    cids = {p.client_id for p in plans if p.client_id}
    oids = {p.owner_id for p in plans}
    clients = (
        dict((await db.execute(select(Client.id, Client.name).where(Client.id.in_(cids)))).tuples().all())
        if cids
        else {}
    )
    owners = (
        dict((await db.execute(select(User.id, User.name).where(User.id.in_(oids)))).tuples().all())
        if oids
        else {}
    )
    return clients, owners


async def plans_out(ctx: RequestContext, db: AsyncSession, plans: list[WorkforcePlan]) -> list[PlanOut]:
    if not plans:
        return []
    ids = [p.id for p in plans]
    clients, owners = await _names(db, plans)
    # current-revision line aggregates
    agg: dict[uuid.UUID, tuple[int, int, Decimal | None]] = {}
    lines = (
        await db.execute(
            select(
                WorkforcePlanLine.workforce_plan_id,
                WorkforcePlanLine.revision,
                func.count(),
                func.coalesce(func.sum(WorkforcePlanLine.headcount), 0),
                func.sum(WorkforcePlanLine.budget_amount),
            )
            .where(WorkforcePlanLine.workforce_plan_id.in_(ids), WorkforcePlanLine.change_kind != "removed")
            .group_by(WorkforcePlanLine.workforce_plan_id, WorkforcePlanLine.revision)
        )
    ).tuples()
    current = {p.id: p.current_revision for p in plans}
    for pid, rev, n, hc, budget in lines:
        if rev == current[pid]:
            agg[pid] = (int(n), int(hc), budget)
    slots: dict[uuid.UUID, SlotCounts] = defaultdict(SlotCounts)
    for pid, status, n in (
        await db.execute(
            select(HeadcountSlot.workforce_plan_id, HeadcountSlot.status, func.count())
            .where(HeadcountSlot.workforce_plan_id.in_(ids))
            .group_by(HeadcountSlot.workforce_plan_id, HeadcountSlot.status)
        )
    ).tuples():
        setattr(slots[pid], status, int(n))
    money = can_view_money(ctx)
    out = []
    for p in plans:
        n, hc, budget = agg.get(p.id, (0, 0, None))
        out.append(
            PlanOut(
                id=p.id,
                name=p.name,
                period_start=p.period_start,
                period_end=p.period_end,
                fiscal_label=p.fiscal_label,
                business_unit_id=p.business_unit_id,
                client_id=p.client_id,
                client_name=clients.get(p.client_id) if p.client_id else None,
                project_id=p.project_id,
                currency=p.currency,
                status=p.status,
                origin=p.origin,
                owner_id=p.owner_id,
                owner_name=owners.get(p.owner_id),
                submitted_by_client_user_id=p.submitted_by_client_user_id,
                approved_revision=p.approved_revision,
                current_revision=p.current_revision,
                approval_request_id=p.approval_request_id,
                notes=p.notes,
                picked_up_at=p.picked_up_at,
                submitted_at=p.submitted_at,
                approved_at=p.approved_at,
                created_at=p.created_at,
                updated_at=p.updated_at,
                line_count=n,
                total_headcount=hc,
                total_budget=(budget.quantize(Decimal("0.01")) if budget is not None else None)
                if money
                else None,
                money_masked=not money,
                slots=slots[p.id],
                editable=p.status in EDITABLE,
            )
        )
    return out


async def plan_out(ctx: RequestContext, db: AsyncSession, plan: WorkforcePlan) -> PlanOut:
    await db.refresh(plan)
    return (await plans_out(ctx, db, [plan]))[0]


# ================================================================ CRUD
async def list_plans(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: str | None = None,
    client_id: uuid.UUID | None = None,
    origin: str | None = None,
    q: str | None = None,
) -> list[PlanOut]:
    stmt = scoped(ctx, select(WorkforcePlan), READ)
    if status:
        stmt = stmt.where(WorkforcePlan.status == status)
    if client_id:
        stmt = stmt.where(WorkforcePlan.client_id == client_id)
    if origin:
        stmt = stmt.where(WorkforcePlan.origin == origin)
    if q and q.strip():
        stmt = stmt.where(WorkforcePlan.name.ilike(f"%{q.strip()[:100]}%"))
    rows = list(
        (
            await db.execute(
                stmt.order_by(WorkforcePlan.period_start.desc(), WorkforcePlan.created_at.desc()).limit(500)
            )
        ).scalars()
    )
    return await plans_out(ctx, db, rows)


async def create_plan(ctx: RequestContext, db: AsyncSession, data: PlanCreate) -> PlanOut:
    _check_period(data.period_start, data.period_end)
    await _check_refs(
        ctx, db, client_id=data.client_id, project_id=data.project_id, business_unit_id=data.business_unit_id
    )
    principal = ctx.principal
    owner = user_id(ctx)
    if owner is None:
        raise invalid("owner_required", "A plan needs an owning User")
    plan = WorkforcePlan(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name,
        period_start=data.period_start,
        period_end=data.period_end,
        fiscal_label=fiscal_label(data.period_start, data.period_end),
        business_unit_id=data.business_unit_id or getattr(principal, "business_unit_id", None),
        client_id=data.client_id,
        project_id=data.project_id,
        currency=(data.currency or await tenant_currency(db)).upper(),
        status="draft",
        origin="internal",
        owner_id=owner,
        current_revision=0,
        notes=data.notes,
    )
    db.add(plan)
    await db.flush()
    return await plan_out(ctx, db, plan)


async def get_plan_out(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> PlanOut:
    return await plan_out(ctx, db, await get_plan(ctx, db, plan_id, permission=READ))


async def update_plan(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, data: PlanUpdate) -> PlanOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status not in EDITABLE:
        raise conflict(
            "plan_not_editable",
            "The plan can only change while Draft, Budgeted or a Revision Draft",
            status=plan.status,
        )
    sent = data.model_dump(exclude_unset=True)
    for key in ("name", "period_start", "period_end", "currency"):
        if key in sent and sent[key] is None:
            raise invalid("field_required", f"{key} cannot be empty", field=key)
    start = sent.get("period_start", plan.period_start)
    end = sent.get("period_end", plan.period_end)
    if "period_start" in sent or "period_end" in sent:
        _check_period(start, end)
        outside = [
            ln.id
            for ln in await lines_of(db, plan.id, plan.current_revision, include_removed=False)
            if ln.period_start < start.replace(day=1) or ln.period_start > end
        ]
        if outside:
            raise invalid(
                "lines_outside_period",
                "Some lines start outside the new period",
                field="period_start",
                lines=[str(i) for i in outside],
            )
    locked_refs = plan.approved_revision is not None
    if locked_refs and any(k in sent for k in ("client_id", "project_id", "currency")):
        raise conflict(
            "plan_refs_locked", "Client, project and currency are fixed once a plan has been approved"
        )
    client_id = sent.get("client_id", plan.client_id)
    project_id = sent.get("project_id", plan.project_id)
    await _check_refs(
        ctx,
        db,
        client_id=client_id if "client_id" in sent or "project_id" in sent else None,
        project_id=project_id if "client_id" in sent or "project_id" in sent else None,
        business_unit_id=sent.get("business_unit_id"),
    )
    for key, value in sent.items():
        setattr(plan, key, value.upper() if key == "currency" and value else value)
    plan.fiscal_label = fiscal_label(plan.period_start, plan.period_end)
    if "client_id" in sent:
        for ln in await lines_of(db, plan.id, plan.current_revision):
            ln.client_id = plan.client_id
    mark_picked_up(ctx, plan)
    await db.flush()
    return await plan_out(ctx, db, plan)


async def delete_plan(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> None:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status not in ("draft", "budgeted", "rejected") or plan.approved_revision is not None:
        raise conflict(
            "plan_not_deletable", "Only a plan that was never approved can be deleted", status=plan.status
        )
    await db.delete(plan)
    await db.flush()
    audit.record("workforce.plan_deleted", "workforce_plan", plan_id, session=db)


def _shift_end(end: date, months: int) -> date:
    shifted = add_months(end, months)
    if end.day == calendar.monthrange(end.year, end.month)[1]:
        return shifted.replace(day=calendar.monthrange(shifted.year, shifted.month)[1])
    return shifted


async def copy_plan(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, data: PlanCopyIn) -> PlanOut:
    """A new Draft plan from ``plan_id``: lines shifted by the period offset (whole months), budgets copied, no
    slots. The source is the approved revision when there is one, else the current revision."""
    source = await get_plan(ctx, db, plan_id, permission=READ)
    owner = user_id(ctx)
    if owner is None:
        raise invalid("owner_required", "A plan needs an owning User")
    offset = months_between(source.period_start, data.period_start)
    start = data.period_start
    end = _shift_end(source.period_end, offset)
    if end <= start:
        end = _shift_end(source.period_end, offset + 1)
    _check_period(start, end)
    plan = WorkforcePlan(
        id=uuid.uuid4(),
        tenant_id=source.tenant_id,
        name=data.name,
        period_start=start,
        period_end=end,
        fiscal_label=fiscal_label(start, end),
        business_unit_id=source.business_unit_id,
        client_id=source.client_id,
        project_id=source.project_id,
        currency=source.currency,
        status="draft",
        origin="internal",
        owner_id=owner,
        current_revision=0,
        notes=source.notes,
    )
    db.add(plan)
    await db.flush()
    revision = source.approved_revision if source.approved_revision is not None else source.current_revision
    for ln in await lines_of(db, source.id, revision, include_removed=False):
        line_start = add_months(ln.period_start, offset).replace(day=1)
        line_end = min(_shift_end(ln.period_end, offset), end)
        if line_start > end or line_start < start.replace(day=1):
            continue
        new_id = uuid.uuid4()
        db.add(
            WorkforcePlanLine(
                id=new_id,
                tenant_id=plan.tenant_id,
                workforce_plan_id=plan.id,
                client_id=plan.client_id,
                revision=0,
                lineage_id=new_id,
                status="forecast",
                change_kind="added",
                role_title=ln.role_title,
                period_start=line_start,
                period_end=max(line_end, line_start),
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
            )
        )
    await db.flush()
    audit.record(
        "workforce.plan_copied", "workforce_plan", plan.id, session=db, source_plan_id=str(source.id)
    )
    return await plan_out(ctx, db, plan)


# ================================================================ simple transitions
def invalid_transition(plan: WorkforcePlan, to: str) -> Exception:
    return conflict(
        "invalid_transition", f"A {plan.status} plan cannot move to {to}", status=plan.status, to=to
    )


async def budget(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> PlanOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status != "draft":
        raise invalid_transition(plan, "budgeted")
    lines = await lines_of(db, plan.id, plan.current_revision, include_removed=False)
    if not lines:
        raise invalid("plan_empty", "Add at least one line before budgeting the plan")
    missing = [ln for ln in lines if ln.budget_amount is None]
    if missing:
        raise invalid(
            "budget_missing",
            "Every line needs a budget before the plan is budgeted",
            lines=[{"id": str(ln.id), "role_title": ln.role_title} for ln in missing],
        )
    unresolved: list[dict[str, Any]] = []
    for ln in lines:
        if any(not s.get("skill_key") for s in ln.skills or []):
            skills, errors = await resolve_skills(ctx, db, list(ln.skills or []))
            if errors:
                unresolved.append(
                    {"id": str(ln.id), "role_title": ln.role_title, "skills": [m for _, m in errors]}
                )
            else:
                ln.skills = skills
    if unresolved:
        raise invalid("skills_unresolved", "Some skills do not match the skill catalogue", lines=unresolved)
    for ln in lines:
        ln.status = "budgeted"
    plan.status = "budgeted"
    mark_picked_up(ctx, plan)
    await db.flush()
    return await plan_out(ctx, db, plan)


async def unbudget(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> PlanOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status != "budgeted":
        raise invalid_transition(plan, "draft")
    for ln in await lines_of(db, plan.id, plan.current_revision):
        ln.status = "forecast"
    plan.status = "draft"
    await db.flush()
    return await plan_out(ctx, db, plan)


async def reopen(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> PlanOut:
    plan = await get_plan(ctx, db, plan_id, permission=WRITE, lock=True)
    if plan.status != "rejected":
        raise invalid_transition(plan, "draft")
    for ln in await lines_of(db, plan.id, plan.current_revision):
        ln.status = "forecast"
    plan.status = "draft"
    await db.flush()
    return await plan_out(ctx, db, plan)
