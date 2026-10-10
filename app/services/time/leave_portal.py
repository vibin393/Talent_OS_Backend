"""Leave in the Worker "My Work" portal and the Client Portal (Epic 26, AD-26 / AD-27).

Every function is scoped by the portal principal: the Worker's ``candidate_id`` (another Worker's ids -> 404)
or the Client's ``client_id`` plus ``visible_placement_ids`` for restricted Client Users. The work runs in an
internal tenant session on behalf of the principal (configs, balances and approvals are internal-only tables);
responses use ``schemas.leave_portal`` only. The Client calendar shows "On leave" -- never the type, reason or
balances -- and only when the Client opted in (else 404).
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.placements import Placement
from app.models.time import LeaveBalance, LeaveRequest, LeaveType
from app.schemas.leave import LeaveRequestIn
from app.schemas.leave_portal import (
    ClientLeaveCalendarDay,
    ClientLeaveCalendarOut,
    ClientLeaveCalendarRow,
    WorkerHolidayOut,
    WorkerHolidaysOut,
    WorkerLeaveBalanceOut,
    WorkerLeavePreviewOut,
    WorkerLeaveRequestOut,
    WorkerLeaveTypeOut,
)
from app.services.time import accruals, holidays, leave_requests
from app.services.time.accruals import q_days
from app.services.time.leave_balances import leave_year_mode
from app.services.time.leave_types import eligible, team_calendar_enabled

ACTIVE = ("active", "offboarding")


def _worker_actor(candidate_id: uuid.UUID) -> ActorRef:
    return ActorRef("candidate", candidate_id)


def _req_out(r: LeaveRequest, type_name: str | None) -> WorkerLeaveRequestOut:
    return WorkerLeaveRequestOut(
        id=r.id,
        human_id=r.human_id,
        placement_id=r.placement_id,
        leave_type_id=r.leave_type_id,
        leave_type_name=type_name,
        start_date=r.start_date,
        end_date=r.end_date,
        start_half=r.start_half,
        end_half=r.end_half,
        days=r.days,
        reason=r.reason,
        status=r.status,
        decided_at=r.decided_at,
        decision_comment=r.decision_comment,
        created_at=r.created_at,
    )


async def _placements(db: AsyncSession, candidate_id: uuid.UUID) -> list[Placement]:
    return list(
        (
            await db.execute(
                select(Placement)
                .where(Placement.candidate_id == candidate_id, Placement.status.in_(ACTIVE))
                .order_by(Placement.created_at.desc())
            )
        ).scalars()
    )


async def my_types(
    ctx: RequestContext, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[WorkerLeaveTypeOut]:
    del ctx
    async with tenant_session(tenant_id) as db:
        placements = await _placements(db, candidate_id)
        types = list(
            (
                await db.execute(
                    select(LeaveType).where(LeaveType.archived_at.is_(None)).order_by(LeaveType.name)
                )
            ).scalars()
        )
        return [
            WorkerLeaveTypeOut(
                id=t.id, code=t.code, name=t.name, paid=t.paid, allow_half_day=t.allow_half_day
            )
            for t in types
            if any(eligible(t, p.worker_type, p.legal_entity_id) for p in placements)
        ]


async def my_balances(
    ctx: RequestContext, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[WorkerLeaveBalanceOut]:
    del ctx
    async with tenant_session(tenant_id) as db:
        year = accruals.leave_year_of(leave_requests_today(), await leave_year_mode(db, tenant_id))
        rows = (
            await db.execute(
                select(LeaveBalance, LeaveType)
                .join(LeaveType, LeaveType.id == LeaveBalance.leave_type_id)
                .where(
                    LeaveBalance.candidate_id == candidate_id,
                    LeaveBalance.leave_year == year,
                    LeaveType.paid.is_(True),
                )
                .order_by(LeaveType.name)
            )
        ).all()
        merged: dict[uuid.UUID, list[Decimal]] = {}
        names: dict[uuid.UUID, str] = {}
        for b, t in rows:
            acc = merged.setdefault(t.id, [Decimal("0"), Decimal("0")])
            acc[0] += Decimal(b.balance)
            acc[1] += Decimal(b.pending)
            names[t.id] = t.name
        return [
            WorkerLeaveBalanceOut(
                leave_type_id=tid,
                leave_type_name=names[tid],
                leave_year=year,
                balance=q_days(v[0]),
                pending=q_days(v[1]),
                available=q_days(v[0] - v[1]),
            )
            for tid, v in merged.items()
        ]


def leave_requests_today() -> date:
    from app.services.time.common import today

    return today()


async def my_requests(
    ctx: RequestContext, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[WorkerLeaveRequestOut]:
    del ctx
    async with tenant_session(tenant_id) as db:
        return [_req_out(r, n) for r, n in await leave_requests.worker_requests(db, candidate_id)]


async def _own(db: AsyncSession, candidate_id: uuid.UUID, request_id: uuid.UUID) -> LeaveRequest:
    req = await leave_requests.get_request(db, request_id, lock=True)
    if req.candidate_id != candidate_id:
        raise NotFound("Leave request not found")
    return req


async def _type_name(db: AsyncSession, leave_type_id: uuid.UUID) -> str | None:
    return (
        await db.execute(select(LeaveType.name).where(LeaveType.id == leave_type_id))
    ).scalar_one_or_none()


async def preview(
    ctx: RequestContext, tenant_id: uuid.UUID, candidate_id: uuid.UUID, data: LeaveRequestIn
) -> WorkerLeavePreviewOut:
    async with tenant_session(tenant_id) as db:
        out = await leave_requests.preview(ctx, db, candidate_id, data)
        return WorkerLeavePreviewOut(
            days=out.days,
            working_dates=out.working_dates,
            available=out.available,
            sufficient=out.sufficient,
            error_code=out.error_code,
        )


async def create(
    ctx: RequestContext, tenant_id: uuid.UUID, candidate_id: uuid.UUID, data: LeaveRequestIn
) -> WorkerLeaveRequestOut:
    async with tenant_session(tenant_id) as db:
        req = await leave_requests.create_request(
            ctx, db, candidate_id, data, actor=_worker_actor(candidate_id)
        )
        return _req_out(req, await _type_name(db, req.leave_type_id))


async def withdraw(
    ctx: RequestContext, tenant_id: uuid.UUID, candidate_id: uuid.UUID, request_id: uuid.UUID
) -> WorkerLeaveRequestOut:
    async with tenant_session(tenant_id) as db:
        req = await _own(db, candidate_id, request_id)
        await leave_requests.withdraw(ctx, db, req, _worker_actor(candidate_id))
        return _req_out(req, await _type_name(db, req.leave_type_id))


async def cancel(
    ctx: RequestContext, tenant_id: uuid.UUID, candidate_id: uuid.UUID, request_id: uuid.UUID
) -> WorkerLeaveRequestOut:
    async with tenant_session(tenant_id) as db:
        req = await _own(db, candidate_id, request_id)
        await leave_requests.request_cancel(ctx, db, req, _worker_actor(candidate_id))
        return _req_out(req, await _type_name(db, req.leave_type_id))


async def my_holidays(
    ctx: RequestContext,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    year: int,
    placement_id: uuid.UUID | None = None,
) -> WorkerHolidaysOut:
    del ctx
    async with tenant_session(tenant_id) as db:
        placements = await _placements(db, candidate_id)
        p = (
            next((x for x in placements if x.id == placement_id), None)
            if placement_id
            else (placements[0] if placements else None)
        )
        if placement_id is not None and p is None:
            raise NotFound("Placement not found")
        if p is None:
            return WorkerHolidaysOut(
                year=year, placement_id=None, optional_allowance=0, optional_selected=0, holidays=[]
            )
        days = await holidays._all_days(db, p.id, date(year, 1, 1), date(year, 12, 31))
        allowance, taken = await holidays.optional_allowance_for(db, p, year)
        return WorkerHolidaysOut(
            year=year,
            placement_id=p.id,
            optional_allowance=allowance,
            optional_selected=taken,
            holidays=[
                WorkerHolidayOut(
                    holiday_id=d.holiday_id,
                    date=d.date,
                    name=d.name,
                    type=d.type,
                    optional=d.optional,
                    selected=d.selected,
                )
                for d in days
            ],
        )


async def set_optional(
    ctx: RequestContext,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    holiday_id: uuid.UUID,
    placement_id: uuid.UUID | None,
    *,
    selected: bool,
) -> WorkerHolidaysOut:
    async with tenant_session(tenant_id) as db:
        placements = await _placements(db, candidate_id)
        p = (
            next((x for x in placements if x.id == placement_id), None)
            if placement_id
            else (placements[0] if placements else None)
        )
        if p is None:
            raise NotFound("Placement not found")
        await holidays.set_optional(ctx, db, candidate_id, p.id, holiday_id, selected=selected)
        h = await holidays._get_holiday(db, holiday_id)
        year, pid = h.holiday_date.year, p.id
    return await my_holidays(ctx, tenant_id, candidate_id, year, pid)


# ---------------------------------------------------------------- Client Portal calendar
async def client_calendar(
    ctx: RequestContext, cdb: AsyncSession, tenant_id: uuid.UUID, client_id: uuid.UUID, start: date, end: date
) -> ClientLeaveCalendarOut:
    from app.core.client_scope import visible_placement_ids
    from app.core.errors import ValidationFailed

    if end < start or (end - start).days > 92:
        raise ValidationFailed("Choose a range of at most three months", code="range_invalid")
    visible = await visible_placement_ids(ctx, cdb)
    async with tenant_session(tenant_id) as db:
        if not await team_calendar_enabled(db, tenant_id, client_id):
            raise NotFound("Not found")
        rows = await leave_requests.calendar_rows(
            db,
            start=start,
            end=end,
            statuses=("approved", "cancellation_requested"),
            client_id=client_id,
            placement_ids=visible,
        )
    out: dict[uuid.UUID, ClientLeaveCalendarRow] = {}
    for req, name, _type_name, working in rows:
        row = out.setdefault(
            req.placement_id, ClientLeaveCalendarRow(worker_name=name, placement_id=req.placement_id, days=[])
        )
        for d in leave_requests.expand(req, working, with_type=None):
            row.days.append(ClientLeaveCalendarDay(date=d.date, half=d.half))
    return ClientLeaveCalendarOut(start=start, end=end, rows=list(out.values()))
