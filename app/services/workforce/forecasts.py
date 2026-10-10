# ruff: noqa: E501
"""Client Portal hiring forecasts (Epic 44, FR-226).

Runs in the Client session (org RLS ``client`` on ``workforce_plans.client_id``; lines inherit the plan's client):
another Client's forecast is 404, and internal plans of the same Client are never listed (``origin`` filter). A
forecast is a Draft plan with ``origin='client_forecast'``, ``client_id`` = the principal's client,
``submitted_by_client_user_id`` and ``owner_id`` = the Client's Account Manager (``clients.owner_id``), who is
notified (``workforce_client_forecast_submitted``). Lines carry no Money; skills are free names, matched to the
catalogue when the Account Manager budgets the plan. The Client may edit / withdraw only while the plan is Draft and
not yet picked up by an internal User (409 ``forecast_locked``). Responses use ``schemas/workforce_portal.py`` only.
"""

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.workforce import ClientForecastSubmitted
from app.models.workforce import WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce_portal import (
    ForecastIn,
    ForecastLineIn,
    ForecastLinePortalOut,
    ForecastPortalDetail,
    ForecastPortalOut,
    ForecastUpdate,
)
from app.services.client_portal_base import client_of
from app.services.workforce.common import conflict, invalid, lines_of, month_key, tenant_currency
from app.services.workforce.plans import _check_period, fiscal_label

ORIGIN = "client_forecast"


def client_status(plan: WorkforcePlan) -> str:
    if plan.status == "approved" or (plan.approved_revision is not None):
        return "approved"
    if plan.status == "rejected":
        return "declined"
    if plan.status == "draft" and plan.picked_up_at is None:
        return "submitted"
    return "in_review"


def editable(plan: WorkforcePlan) -> bool:
    return plan.status == "draft" and plan.picked_up_at is None


def _summary(plan: WorkforcePlan, n: int, hc: int) -> dict[str, Any]:
    return {
        "id": plan.id,
        "name": plan.name,
        "period_start": plan.period_start,
        "period_end": plan.period_end,
        "status": client_status(plan),
        "created_at": plan.created_at,
        "line_count": n,
        "total_headcount": hc,
        "editable": editable(plan),
    }


def _line_out(ln: WorkforcePlanLine) -> ForecastLinePortalOut:
    return ForecastLinePortalOut(
        id=ln.id,
        role_title=ln.role_title,
        skills=[str(s.get("name") or "") for s in ln.skills or []],
        headcount=ln.headcount,
        location_text=ln.location_text,
        planned_start_month=month_key(ln.period_start),
        comment=ln.client_comment,
    )


async def _plan(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, *, lock: bool = False
) -> WorkforcePlan:
    _, client_id, _ = client_of(ctx)
    stmt = select(WorkforcePlan).where(
        WorkforcePlan.id == plan_id, WorkforcePlan.origin == ORIGIN, WorkforcePlan.client_id == client_id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Forecast not found")
    return row


async def _visible_lines(db: AsyncSession, plan: WorkforcePlan) -> list[WorkforcePlanLine]:
    revision = plan.approved_revision if plan.approved_revision is not None else plan.current_revision
    return await lines_of(db, plan.id, revision, include_removed=False)


async def _detail(db: AsyncSession, plan: WorkforcePlan) -> ForecastPortalDetail:
    lines = await _visible_lines(db, plan)
    return ForecastPortalDetail(
        **_summary(plan, len(lines), sum(ln.headcount for ln in lines)), lines=[_line_out(ln) for ln in lines]
    )


def _check_lines(data_start: Any, data_end: Any, lines: list[ForecastLineIn]) -> None:
    for i, ln in enumerate(lines):
        if ln.planned_start_month < data_start.replace(day=1) or ln.planned_start_month > data_end:
            raise invalid(
                "start_month_outside_period",
                "Each line must start inside the forecast period",
                field=f"lines.{i}.planned_start_month",
            )


def _add_lines(db: AsyncSession, plan: WorkforcePlan, lines: list[ForecastLineIn]) -> None:
    for ln in lines:
        line_id = uuid.uuid4()
        start = ln.planned_start_month
        db.add(
            WorkforcePlanLine(
                id=line_id,
                tenant_id=plan.tenant_id,
                workforce_plan_id=plan.id,
                client_id=plan.client_id,
                revision=0,
                lineage_id=line_id,
                status="forecast",
                change_kind="added",
                role_title=ln.role_title,
                period_start=start,
                period_end=max(plan.period_end, start),
                headcount=ln.headcount,
                skills=[{"skill_key": None, "name": s.name, "min_proficiency": None} for s in ln.skills],
                line_type="new",
                location_text=ln.location_text,
                client_comment=ln.notes,
            )
        )


# ================================================================ endpoints
async def client_list(ctx: RequestContext, db: AsyncSession) -> list[ForecastPortalOut]:
    _, client_id, _ = client_of(ctx)
    plans = list(
        (
            await db.execute(
                select(WorkforcePlan)
                .where(WorkforcePlan.origin == ORIGIN, WorkforcePlan.client_id == client_id)
                .order_by(WorkforcePlan.created_at.desc())
            )
        ).scalars()
    )
    agg: dict[uuid.UUID, tuple[int, int]] = defaultdict(lambda: (0, 0))
    if plans:
        wanted = {
            p.id: (p.approved_revision if p.approved_revision is not None else p.current_revision)
            for p in plans
        }
        for pid, rev, n, hc in (
            await db.execute(
                select(
                    WorkforcePlanLine.workforce_plan_id,
                    WorkforcePlanLine.revision,
                    func.count(),
                    func.coalesce(func.sum(WorkforcePlanLine.headcount), 0),
                )
                .where(
                    WorkforcePlanLine.workforce_plan_id.in_(list(wanted)),
                    WorkforcePlanLine.change_kind != "removed",
                )
                .group_by(WorkforcePlanLine.workforce_plan_id, WorkforcePlanLine.revision)
            )
        ).tuples():
            if wanted[pid] == rev:
                agg[pid] = (int(n), int(hc))
    return [ForecastPortalOut(**_summary(p, *agg[p.id])) for p in plans]


async def client_get(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> ForecastPortalDetail:
    return await _detail(db, await _plan(ctx, db, plan_id))


async def client_create(ctx: RequestContext, db: AsyncSession, data: ForecastIn) -> ForecastPortalDetail:
    from app.models.clients import Client

    tenant_id, client_id, me = client_of(ctx)
    _check_period(data.period_start, data.period_end)
    _check_lines(data.period_start, data.period_end, data.lines)
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise NotFound("Client not found")
    plan = WorkforcePlan(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=data.name,
        period_start=data.period_start,
        period_end=data.period_end,
        fiscal_label=fiscal_label(data.period_start, data.period_end),
        client_id=client_id,
        currency=await tenant_currency(db),
        status="draft",
        origin=ORIGIN,
        owner_id=client.owner_id,
        submitted_by_client_user_id=me,
        current_revision=0,
    )
    db.add(plan)
    await db.flush()
    _add_lines(db, plan, data.lines)
    await db.flush()
    emit(db, ClientForecastSubmitted(tenant_id=tenant_id, workforce_plan_id=plan.id, client_id=client_id))
    audit.record(
        "workforce.client_forecast_submitted", "workforce_plan", plan.id, session=db, lines=len(data.lines)
    )
    await db.refresh(plan)
    return await _detail(db, plan)


async def client_update(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, data: ForecastUpdate
) -> ForecastPortalDetail:
    plan = await _plan(ctx, db, plan_id, lock=True)
    if not editable(plan):
        raise conflict("forecast_locked", "Your Account Manager is already working on this forecast")
    sent = data.model_dump(exclude_unset=True)
    for key in ("name", "period_start", "period_end"):
        if key in sent and sent[key] is None:
            raise invalid("field_required", f"{key} cannot be empty", field=key)
    start = sent.get("period_start", plan.period_start)
    end = sent.get("period_end", plan.period_end)
    _check_period(start, end)
    if data.lines is not None:
        _check_lines(start, end, data.lines)
    plan.name = sent.get("name", plan.name)
    plan.period_start, plan.period_end = start, end
    plan.fiscal_label = fiscal_label(start, end)
    if data.lines is not None:
        for ln in await lines_of(db, plan.id, plan.current_revision):
            await db.delete(ln)
        await db.flush()
        _add_lines(db, plan, data.lines)
    else:
        for ln in await lines_of(db, plan.id, plan.current_revision):
            if ln.period_start < start.replace(day=1) or ln.period_start > end:
                raise invalid(
                    "start_month_outside_period",
                    "Each line must start inside the forecast period",
                    field="period_start",
                )
            if ln.period_end > end:
                ln.period_end = max(end, ln.period_start)
    await db.flush()
    await db.refresh(plan)
    return await _detail(db, plan)


async def client_delete(ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID) -> None:
    plan = await _plan(ctx, db, plan_id, lock=True)
    if not editable(plan):
        raise conflict("forecast_locked", "Your Account Manager is already working on this forecast")
    await db.delete(plan)
    await db.flush()
    audit.record("workforce.client_forecast_withdrawn", "workforce_plan", plan_id, session=db)
