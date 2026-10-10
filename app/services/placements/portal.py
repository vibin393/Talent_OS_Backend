"""Worker ("My Work") portal (Epic 19 A8; AD-26/27, fail-closed). Sessions run with the worker org scope (R17):
every read is filtered by RLS *and* explicitly by ``candidate_id`` (another Worker's id -> 404). Responses use
``schemas.placements_portal`` (never bill / vendor rates, margin, burden or internal notes).

Writes that need internal-only tables (approval requests) run in an internal tenant session on behalf of the
Worker (``request_bank_change``); names of clients are read the same way (display names only).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.placements import Placement, RateSegment, WorkerBankAccount, WorkerProfile
from app.schemas.placements_api import BankAccountIn
from app.schemas.placements_portal import (
    PortalBankAccountOut,
    PortalPlacementOut,
    PortalProfileOut,
    PortalProfileUpdate,
)
from app.services.placements import joining, workers
from app.services.placements.common import names


async def _client_names(tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    async with tenant_session(tenant_id) as internal:
        _, clients = await names(internal, client_ids=ids)
    return clients


async def _own(db: AsyncSession, candidate_id: uuid.UUID, placement_id: uuid.UUID) -> Placement:
    p = (
        await db.execute(
            select(Placement).where(Placement.id == placement_id, Placement.candidate_id == candidate_id)
        )
    ).scalar_one_or_none()
    if p is None:
        raise NotFound("Placement not found")
    return p


async def _current_pay(db: AsyncSession, p: Placement) -> RateSegment | None:
    return (
        await db.execute(
            select(RateSegment)
            .where(RateSegment.placement_id == p.id, RateSegment.status == "active")
            .order_by(RateSegment.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _out(db: AsyncSession, p: Placement, client_name: str | None) -> PortalPlacementOut:
    seg = await _current_pay(db, p)
    return PortalPlacementOut(
        id=p.id,
        human_id=p.human_id,
        status=p.status,
        client_name=client_name,
        role_title=p.role_title,
        work_location_text=p.work_location_text,
        planned_start_date=p.planned_start_date,
        planned_end_date=p.planned_end_date,
        actual_start_date=p.actual_start_date,
        joining_confirmed_at=p.joining_confirmed_at,
        can_confirm_joining=p.status == "pre_joining",
        pay_rate=seg.pay_rate if seg and p.payee_kind == "worker" else None,
        rate_unit=seg.rate_unit if seg else None,
        currency=p.currency,
    )


async def list_placements(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[PortalPlacementOut]:
    rows = list(
        (
            await db.execute(
                select(Placement)
                .where(Placement.candidate_id == candidate_id, Placement.status != "dropped")
                .order_by(Placement.planned_start_date.desc())
            )
        ).scalars()
    )
    clients = await _client_names(tenant_id, {p.client_id for p in rows})
    return [await _out(db, p, clients.get(p.client_id)) for p in rows]


async def get_placement(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, placement_id: uuid.UUID
) -> PortalPlacementOut:
    p = await _own(db, candidate_id, placement_id)
    clients = await _client_names(tenant_id, {p.client_id})
    return await _out(db, p, clients.get(p.client_id))


async def confirm_joining(
    ctx: RequestContext,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    placement_id: uuid.UUID,
) -> PortalPlacementOut:
    p = (
        await db.execute(
            select(Placement)
            .where(Placement.id == placement_id, Placement.candidate_id == candidate_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if p is None:
        raise NotFound("Placement not found")
    await joining.confirm_joining(ctx, db, p, ActorRef("candidate", candidate_id), "portal")
    clients = await _client_names(tenant_id, {p.client_id})
    return await _out(db, p, clients.get(p.client_id))


async def get_profile(db: AsyncSession, candidate_id: uuid.UUID) -> PortalProfileOut:
    from app.services import entity_refs

    profile = (
        await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == candidate_id))
    ).scalar_one_or_none()
    who = await entity_refs.addressee(db, "candidate", candidate_id)
    return PortalProfileOut(
        worker_human_id=profile.worker_human_id if profile else None,
        full_name=who.full_name if who else None,
        email=who.address if who else None,
        emergency_name=profile.emergency_name if profile else None,
        emergency_relation=profile.emergency_relation if profile else None,
        emergency_phone=profile.emergency_phone if profile else None,
        pan_last4=profile.pan_last4 if profile else None,
    )


async def update_profile(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: PortalProfileUpdate
) -> PortalProfileOut:
    profile = (
        await db.execute(select(WorkerProfile).where(WorkerProfile.candidate_id == candidate_id))
    ).scalar_one_or_none()
    if profile is None:
        raise NotFound("Worker profile not found")
    await workers.update_profile(ctx, db, candidate_id, data.model_dump(exclude_unset=True))
    return await get_profile(db, candidate_id)


def _bank_out(r: WorkerBankAccount) -> PortalBankAccountOut:
    return PortalBankAccountOut(
        id=r.id,
        holder_name=r.holder_name,
        account_number_last4=r.account_number_last4,
        ifsc=r.ifsc,
        bank_name=r.bank_name,
        status=r.status,
        created_at=r.created_at,
    )


async def bank_accounts(db: AsyncSession, candidate_id: uuid.UUID) -> list[PortalBankAccountOut]:
    rows = (
        await db.execute(
            select(WorkerBankAccount)
            .where(
                WorkerBankAccount.candidate_id == candidate_id,
                WorkerBankAccount.status.in_(("active", "pending_approval", "rejected")),
            )
            .order_by(WorkerBankAccount.created_at.desc())
        )
    ).scalars()
    return [_bank_out(r) for r in rows]


async def request_bank_change(
    ctx: RequestContext, db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, data: BankAccountIn
) -> PortalBankAccountOut:
    profile = (
        await db.execute(select(WorkerProfile.id).where(WorkerProfile.candidate_id == candidate_id))
    ).scalar_one_or_none()
    if profile is None:
        raise NotFound("Worker profile not found")
    async with tenant_session(tenant_id) as internal:
        row = await workers.request_bank_change(ctx, internal, candidate_id, data, "worker_portal")
        out = _bank_out(row)
    return out


# ================================================================ Client Portal (Epic 18, FR-103; append-only)
async def _client_rows(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID] | None = None) -> list[Placement]:
    from app.services.client_portal_base import client_of, placement_scope

    _, client_id, _ = client_of(ctx)
    stmt = select(Placement).where(Placement.client_id == client_id)
    if ids is not None:
        stmt = stmt.where(Placement.id.in_(ids))
    scope = await placement_scope(ctx, db)
    if scope is not None:
        stmt = stmt.where(Placement.id.in_(scope or [uuid.uuid4()]))
    return list((await db.execute(stmt.order_by(Placement.planned_start_date.desc()).limit(1000))).scalars())


async def _client_project(db: AsyncSession, rows: list[Placement]) -> list["ClientPlacementOut"]:
    from datetime import UTC, datetime, timedelta

    from app.models.candidates import Candidate
    from app.models.time import Timesheet
    from app.schemas.client_hub_portal import ClientPlacementOut

    if not rows:
        return []
    names = {
        c.id: c.full_name
        for c in (await db.execute(select(Candidate).where(Candidate.id.in_({r.candidate_id for r in rows})))).scalars()
    }
    latest: dict[uuid.UUID, str] = {}
    for ts in (
        await db.execute(
            select(Timesheet).where(Timesheet.placement_id.in_([r.id for r in rows])).order_by(Timesheet.period_start)
        )
    ).scalars():
        latest[ts.placement_id] = ts.status
    soon = datetime.now(UTC).date() + timedelta(days=30)
    out = []
    for r in rows:
        end = r.end_date or r.planned_end_date
        out.append(
            ClientPlacementOut(
                id=r.id,
                human_id=r.human_id,
                worker_name=names.get(r.candidate_id) or "Worker",
                role_title=r.role_title,
                start_date=r.actual_start_date or r.planned_start_date,
                end_date=end,
                status=r.status,
                ending_soon=r.status == "active" and end is not None and end <= soon,
                timesheet_status=latest.get(r.id),
            )
        )
    return out


async def client_placements(ctx: RequestContext, db: AsyncSession, status: str | None = None) -> list["ClientPlacementOut"]:
    """The client's workforce within ``visible_placement_ids`` (never rates, bank / ID data or vendor identity)."""
    rows = await _client_rows(ctx, db)
    if status:
        rows = [r for r in rows if r.status == status]
    return await _client_project(db, rows)


async def client_placement_detail(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> "ClientPlacementDetail":
    from app.models.time import Timesheet
    from app.schemas.client_hub_portal import ClientPlacementDetail, ClientTimesheetBrief

    rows = await _client_rows(ctx, db, [placement_id])
    if not rows:
        raise NotFound("Placement not found")
    base = (await _client_project(db, rows))[0]
    sheets = (
        await db.execute(
            select(Timesheet)
            .where(Timesheet.placement_id == placement_id, Timesheet.status != "draft")
            .order_by(Timesheet.period_start.desc())
            .limit(12)
        )
    ).scalars()
    return ClientPlacementDetail(
        **base.model_dump(),
        work_location_text=rows[0].work_location_text,
        timesheets=[
            ClientTimesheetBrief(id=t.id, period_start=t.period_start, period_end=t.period_end, status=t.status)
            for t in sheets
        ],
    )


import typing  # noqa: E402

if typing.TYPE_CHECKING:
    from app.schemas.client_hub_portal import ClientPlacementDetail, ClientPlacementOut
