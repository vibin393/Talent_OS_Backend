# ruff: noqa: E501
"""Transfer confirmation (Epic 41 A4, FR-211) -- human only (*, FR-257).

``confirm`` (``mobility:transfer`` + ``core.actors.require_human``; 403 ``human_only_action`` for AI / workflow /
system / API-key actors). The Application must be at ``selected`` with an approved overlay (else 409
``not_selected``).

* **Worker** (one transaction; ``SELECT ... FOR UPDATE`` on the current Placement): ``effective_date`` after today
  and after the current start; no open offboarding / extension case (409 ``offboarding_in_progress`` /
  ``extension_open``); ``placements_service.create_direct`` (planned start = effective date) then P2
  ``offboarding.initiate(reason='other', last_working_day=effective - 1 day, reason_note='Internal transfer to
  <new>')`` -- P2 has no ``internal_transfer`` reason, ``other`` + note avoids a P2 CHECK change. Any failure rolls
  back both (savepoint). Old LWD + 1 day == new planned start: no gap, no overlap.
* **User**: only the transfer record (``to_team_id``, ``to_role_title``); team changes are made by an admin (no
  HRIS effects).

Then overlay ``transfer_confirmed``, Application -> ``hired`` (system move), ``InternalTransferConfirmed`` emitted
once, audit ``mobility.transfer_confirmed``.
"""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.events import emit
from app.core.permissions import can_view_compensation
from app.events.mobility import InternalTransferConfirmed
from app.models.mobility import InternalTransfer, MobilityApplication
from app.schemas.mobility import (
    CurrentPlacementOut,
    InternalApplicationOut,
    TransferConfirmIn,
    TransferContextOut,
    TransferOut,
)
from app.services.mobility.common import TRANSFER, conflict, invalid, now, require, today


def transfer_out(t: InternalTransfer, to_human_id: str | None = None) -> TransferOut:
    return TransferOut(
        id=t.id,
        mobility_application_id=t.mobility_application_id,
        employee_kind=t.employee_kind,  # type: ignore[arg-type]
        candidate_id=t.candidate_id,
        user_id=t.user_id,
        from_placement_id=t.from_placement_id,
        to_placement_id=t.to_placement_id,
        to_placement_human_id=to_human_id,
        effective_date=t.effective_date,
        previous_last_working_day=t.effective_date - timedelta(days=1) if t.from_placement_id else None,
        from_team_id=t.from_team_id,
        to_team_id=t.to_team_id,
        to_role_title=t.to_role_title,
        confirmed_by=t.confirmed_by,
        confirmed_at=t.confirmed_at,
        note=t.note,
    )


async def transfers_for(db: AsyncSession, mobility_application_ids: list[uuid.UUID]) -> dict[uuid.UUID, TransferOut]:
    from app.models.placements import Placement

    if not mobility_application_ids:
        return {}
    rows = list(
        (await db.execute(select(InternalTransfer).where(InternalTransfer.mobility_application_id.in_(mobility_application_ids)))).scalars()
    )
    new_ids = {t.to_placement_id for t in rows if t.to_placement_id}
    humans = dict((await db.execute(select(Placement.id, Placement.human_id).where(Placement.id.in_(new_ids)))).tuples().all()) if new_ids else {}
    return {t.mobility_application_id: transfer_out(t, humans.get(t.to_placement_id)) for t in rows}


# ---------------------------------------------------------------- context (dialog)
async def _stage_key(db: AsyncSession, application_id: uuid.UUID) -> str | None:
    from app.models.pipeline import Application, PipelineStage

    return (
        await db.execute(
            select(PipelineStage.key).join(Application, Application.stage_id == PipelineStage.id).where(Application.id == application_id)
        )
    ).scalar_one_or_none()


async def _current(db: AsyncSession, row: MobilityApplication, *, lock: bool = False) -> Any:
    from app.models.placements import Placement
    from app.services.mobility.employees import current_placement

    if row.employee_kind != "worker":
        return None
    pid = row.current_placement_id
    if pid is None:
        p = await current_placement(db, row.candidate_id)
        pid = getattr(p, "id", None)
    if pid is None:
        return None
    return await db.get(Placement, pid, with_for_update=lock)


def _blocking(row: MobilityApplication, stage_key: str | None, current: Any, open_off: bool, open_ext: bool) -> str | None:
    if row.status == "transfer_confirmed":
        return "The transfer is already confirmed."
    if row.status != "approved" or stage_key != "selected":
        return "Move the application to Selected (after manager approval) before confirming the transfer."
    if row.employee_kind == "worker":
        if current is None or current.status != "active":
            return "The worker's current placement is not active."
        if open_off:
            return "The current placement already has an open offboarding case."
        if open_ext:
            return "The current placement has an open extension."
    return None


async def context(ctx: RequestContext, db: AsyncSession, mobility_application_id: uuid.UUID) -> TransferContextOut:
    from app.models.demands import Demand
    from app.services.mobility.applications import visible_row
    from app.services.placements import extension_checks, extensions
    from app.services.placements.service import _active_on

    require(ctx, TRANSFER)
    row = await visible_row(ctx, db, mobility_application_id)
    stage_key = await _stage_key(db, row.application_id)
    demand = await db.get(Demand, row.demand_id)
    current = await _current(db, row)
    cur_out = None
    open_off = open_ext = False
    earliest = today() + timedelta(days=1)
    if current is not None:
        open_off = await extension_checks.open_case(db, current.id) is not None
        open_ext = await extensions.open_extension(db, current.id) is not None
        seg = await _active_on(db, current.id, today())
        show = can_view_compensation(ctx.principal)
        cur_out = CurrentPlacementOut(
            id=current.id,
            human_id=current.human_id,
            status=current.status,
            role_title=current.role_title,
            actual_start_date=current.actual_start_date,
            planned_start_date=current.planned_start_date,
            end_date=current.end_date,
            legal_entity_id=current.legal_entity_id,
            worker_type=current.worker_type,
            engagement_kind=current.engagement_kind,
            currency=current.currency,
            rate_unit=seg.rate_unit if seg else None,
            pay_rate=seg.pay_rate if seg and show else None,
            bill_rate=seg.bill_rate if seg and show else None,
            burden_pct=getattr(seg, "burden_pct", None) if seg and show else None,
            open_offboarding=open_off,
            open_extension=open_ext,
        )
        if current.actual_start_date is not None:
            earliest = max(earliest, current.actual_start_date + timedelta(days=1))
    blocking = _blocking(row, stage_key, current, open_off, open_ext)
    return TransferContextOut(
        mobility_application_id=row.id,
        employee_kind=row.employee_kind,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        stage_key=stage_key,
        can_confirm=blocking is None,
        blocking_reason=blocking,
        demand_id=row.demand_id,
        demand_title=demand.title if demand else None,
        client_id=demand.client_id if demand else None,
        current_placement=cur_out,
        earliest_effective_date=earliest,
    )


# ---------------------------------------------------------------- confirm (*)
async def _worker_transfer(ctx: RequestContext, db: AsyncSession, row: MobilityApplication, demand: Any, data: TransferConfirmIn) -> tuple[uuid.UUID, uuid.UUID]:
    from app.schemas.placements import PlacementCreateIn
    from app.schemas.placements_api import PlacementUpdate
    from app.services import placements_service
    from app.services.placements import creation, extension_checks, extensions, offboarding
    from app.services.placements.service import _active_on

    current = await _current(db, row, lock=True)
    if current is None:
        raise conflict("placement_not_active", "The worker's current placement is not active")
    if await extension_checks.open_case(db, current.id) is not None:
        raise conflict("offboarding_in_progress", "The current placement already has an open offboarding case")
    if await extensions.open_extension(db, current.id) is not None:
        raise conflict("extension_open", "The current placement has an open extension")
    if current.status != "active":
        raise conflict("placement_not_active", "The worker's current placement is not active")
    if current.actual_start_date is not None and data.effective_date <= current.actual_start_date:
        raise invalid("effective_date_invalid", "The transfer must take effect after the current placement started")
    seg = await _active_on(db, current.id, today())
    pay = data.pay_rate if data.pay_rate is not None else (seg.pay_rate if seg else None)
    bill = data.bill_rate if data.bill_rate is not None else (seg.bill_rate if seg else None)
    burden = data.burden_pct if data.burden_pct is not None else (getattr(seg, "burden_pct", None) if seg else None)
    unit = data.rate_unit or (seg.rate_unit if seg and seg.rate_unit in ("hour", "day", "month", "fixed") else "hour")
    async with db.begin_nested():  # one unit: the new Placement and the offboarding case land together or not at all
        new = await placements_service.create_direct(
            ctx,
            db,
            PlacementCreateIn(
                candidate_id=row.candidate_id,
                client_id=demand.client_id,
                legal_entity_id=data.legal_entity_id or current.legal_entity_id,
                application_id=row.application_id,
                demand_id=row.demand_id,
                worker_type=data.worker_type or current.worker_type or "employee",
                engagement_kind=data.engagement_kind or current.engagement_kind,
                planned_start_date=data.effective_date,
                currency=data.currency or current.currency,
                work_state_code=current.work_state_code,
                work_timezone=current.work_timezone,
                rate_unit=unit,
                pay_rate=pay,
                bill_rate=bill,
                burden_pct=burden,
                account_manager_user_id=data.account_manager_user_id or current.account_manager_user_id,
                recruiter_user_id=demand.owner_id,
            ),
        )
        fresh = await creation.get_placement(db, new.id)
        await creation.update_placement(
            ctx,
            db,
            new.id,
            PlacementUpdate(
                lock_version=fresh.lock_version,
                role_title=data.to_role_title or demand.title,
                work_location_text=data.work_location_text or demand.location or current.work_location_text,
            ),
        )
        await offboarding.initiate(
            ctx,
            db,
            current.id,
            "other",
            data.effective_date - timedelta(days=1),
            reason_note=f"Internal transfer to {new.human_id}",
        )
    return current.id, new.id


async def _user_team(db: AsyncSession, user_id: uuid.UUID | None) -> uuid.UUID | None:
    from app.models.identity import TeamMember

    if user_id is None:
        return None
    return (
        await db.execute(select(TeamMember.team_id).where(TeamMember.user_id == user_id).order_by(TeamMember.team_id).limit(1))
    ).scalar_one_or_none()


async def confirm(ctx: RequestContext, db: AsyncSession, mobility_application_id: uuid.UUID, data: TransferConfirmIn) -> InternalApplicationOut:
    from app.models.demands import Demand
    from app.services.mobility.applications import internal_views, visible_row
    from app.services.pipeline import stages
    from app.services.pipeline.applications import system_move

    actor = actor_from_ctx(ctx)
    require_human(actor)  # FR-257: 403 human_only_action for AI / workflow / system / API-key actors
    require(ctx, TRANSFER)
    row = await visible_row(ctx, db, mobility_application_id, lock=True)
    if row.status == "transfer_confirmed":
        raise conflict("transfer_exists", "The transfer is already confirmed")
    stage_key = await _stage_key(db, row.application_id)
    if row.status != "approved" or stage_key != "selected":
        raise conflict("not_selected", "Move the application to Selected before confirming the transfer", stage=stage_key, status=row.status)
    if data.effective_date <= today():
        raise invalid("effective_date_invalid", "The transfer must take effect after today")
    demand = await db.get(Demand, row.demand_id)
    assert demand is not None
    from_placement = to_placement = None
    if row.employee_kind == "worker":
        from_placement, to_placement = await _worker_transfer(ctx, db, row, demand, data)
    t = InternalTransfer(
        id=uuid.uuid4(),
        tenant_id=row.tenant_id,
        mobility_application_id=row.id,
        employee_kind=row.employee_kind,
        candidate_id=row.candidate_id,
        user_id=row.user_id,
        from_placement_id=from_placement,
        to_placement_id=to_placement,
        effective_date=data.effective_date,
        from_team_id=await _user_team(db, row.user_id) if row.employee_kind == "user" else None,
        to_team_id=data.to_team_id if row.employee_kind == "user" else None,
        to_role_title=data.to_role_title or demand.title,
        confirmed_by=actor.id,  # type: ignore[arg-type]
        confirmed_at=now(),
        note=data.note or None,
    )
    db.add(t)
    row.status = "transfer_confirmed"
    await db.flush()
    await system_move(ctx, db, row.application_id, await stages.stage_by_key(db, "hired"), "Internal transfer confirmed")
    audit.record(
        "mobility.transfer_confirmed",
        "mobility_application",
        row.id,
        session=db,
        transfer_id=str(t.id),
        employee_kind=row.employee_kind,
        from_placement_id=str(from_placement) if from_placement else None,
        new_placement_id=str(to_placement) if to_placement else None,
        effective_date=data.effective_date.isoformat(),
    )
    emit(
        db,
        InternalTransferConfirmed(
            tenant_id=row.tenant_id,
            application_id=row.application_id,
            demand_id=row.demand_id,
            candidate_id=row.candidate_id,
            employee_kind=row.employee_kind,
            user_id=row.user_id,
            from_placement_id=from_placement,
            new_placement_id=to_placement,
        ),
    )
    return (await internal_views(db, [row]))[0]


def last_working_day(effective: date) -> date:
    return effective - timedelta(days=1)
