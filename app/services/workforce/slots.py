# ruff: noqa: D401, E501
"""Headcount slots (Epic 44, FR-224, PRD 5.6): creation on approval, cancel, and the event handlers.

Lifecycle: ``open -> requisitioned -> filled``; ``open -> cancelled`` (reason, ``workforce_plan:write``). A converted
slot stays ``open`` with ``demand_id`` set (draft Demand linked). Status changes come only from this module:

* ``DemandOpened`` -> the slot holding the Demand becomes ``requisitioned``; a Demand opened with
  ``workforce_plan_line_id`` but no slot (created manually) claims the lowest open unconverted slot of that line.
* ``PlacementActivated`` -> the slot of the Placement's Demand becomes ``filled``.
* ``DemandStatusChanged`` to ``cancelled`` / ``closed`` without a Placement -> the slot is released (back to
  ``open``, Demand link cleared; audit ``workforce.slot_released``).

``InternalTransferConfirmed`` (Epic 41) is informational only: a transfer fills a slot through ``PlacementActivated``.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.models.workforce import HeadcountSlot, WorkforcePlan, WorkforcePlanLine
from app.schemas.workforce import SlotOut
from app.services.workforce.common import READ, WRITE, conflict, effective_line_for, get_plan, month_key, now

COMMITTED = ("requisitioned", "filled")


# ================================================================ creation / cancellation (approval)
async def create_slots(db: AsyncSession, plan: WorkforcePlan, lineage_id: uuid.UUID, count: int) -> int:
    if count <= 0:
        return 0
    top = (
        await db.execute(
            select(func.coalesce(func.max(HeadcountSlot.seq), 0)).where(
                HeadcountSlot.plan_line_id == lineage_id
            )
        )
    ).scalar_one()
    for i in range(1, count + 1):
        db.add(
            HeadcountSlot(
                id=uuid.uuid4(),
                tenant_id=plan.tenant_id,
                workforce_plan_id=plan.id,
                plan_line_id=lineage_id,
                seq=int(top) + i,
                status="open",
            )
        )
    await db.flush()
    return count


async def lineage_slots(
    db: AsyncSession, lineage_id: uuid.UUID, *, lock: bool = False
) -> list[HeadcountSlot]:
    stmt = select(HeadcountSlot).where(HeadcountSlot.plan_line_id == lineage_id).order_by(HeadcountSlot.seq)
    if lock:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt)).scalars())


def free_slots(slots: list[HeadcountSlot]) -> list[HeadcountSlot]:
    """Open slots without a Demand (cancellable on a headcount decrease), highest ``seq`` first."""
    return sorted((s for s in slots if s.status == "open" and s.demand_id is None), key=lambda s: -s.seq)


def committed(slots: list[HeadcountSlot]) -> list[HeadcountSlot]:
    """Slots holding a Demand (draft, requisitioned) or a Placement: never cancelled by a revision."""
    return [s for s in slots if s.status in COMMITTED or (s.status == "open" and s.demand_id is not None)]


def cancel(slot: HeadcountSlot, reason: str) -> None:
    slot.status = "cancelled"
    slot.cancelled_at = now()
    slot.cancel_reason = reason[:500]


async def refresh_line_status(db: AsyncSession, plan: WorkforcePlan, lineage_id: uuid.UUID) -> None:
    """The effective line is ``requisitioned`` once any slot is requisitioned / filled, else ``approved``."""
    line = await effective_line_for(db, plan, lineage_id)
    if line is None or line.status == "cancelled":
        return
    n = (
        await db.execute(
            select(func.count()).where(
                HeadcountSlot.plan_line_id == lineage_id, HeadcountSlot.status.in_(COMMITTED)
            )
        )
    ).scalar_one()
    line.status = "requisitioned" if n else "approved"


# ================================================================ reads / cancel endpoint
async def slots_out(db: AsyncSession, plan: WorkforcePlan, slots: list[HeadcountSlot]) -> list[SlotOut]:
    from app.models.demands import Demand

    lineages = {s.plan_line_id for s in slots}
    effective: dict[uuid.UUID, WorkforcePlanLine] = {}
    if lineages and plan.approved_revision is not None:
        for ln in (
            await db.execute(
                select(WorkforcePlanLine).where(
                    WorkforcePlanLine.workforce_plan_id == plan.id,
                    WorkforcePlanLine.revision == plan.approved_revision,
                    WorkforcePlanLine.lineage_id.in_(lineages),
                )
            )
        ).scalars():
            effective[ln.lineage_id or ln.id] = ln
    roots: dict[uuid.UUID, WorkforcePlanLine] = {}
    missing = lineages - set(effective)
    if missing:
        roots = {
            ln.id: ln
            for ln in (
                await db.execute(select(WorkforcePlanLine).where(WorkforcePlanLine.id.in_(missing)))
            ).scalars()
        }
    demand_ids = {s.demand_id for s in slots if s.demand_id}
    demands: dict[uuid.UUID, tuple[str, str]] = {}
    if demand_ids:
        demands = {
            i: (r, st)
            for i, r, st in (
                await db.execute(
                    select(Demand.id, Demand.req_id, Demand.status).where(Demand.id.in_(demand_ids))
                )
            ).tuples()
        }
    out = []
    for s in slots:
        row: WorkforcePlanLine | None = effective.get(s.plan_line_id) or roots.get(s.plan_line_id)
        d = demands.get(s.demand_id) if s.demand_id else None
        out.append(
            SlotOut(
                id=s.id,
                workforce_plan_id=s.workforce_plan_id,
                plan_line_id=s.plan_line_id,
                line_id=row.id if row else None,
                role_title=row.role_title if row else None,
                start_month=month_key(row.period_start) if row else None,
                seq=s.seq,
                status=s.status,
                demand_id=s.demand_id,
                demand_human_id=d[0] if d else None,
                demand_status=d[1] if d else None,
                placement_id=s.placement_id,
                converted_at=s.converted_at,
                requisitioned_at=s.requisitioned_at,
                filled_at=s.filled_at,
                cancelled_at=s.cancelled_at,
                cancel_reason=s.cancel_reason,
            )
        )
    return out


async def list_slots(
    ctx: RequestContext, db: AsyncSession, plan_id: uuid.UUID, status: str | None = None
) -> list[SlotOut]:
    plan = await get_plan(ctx, db, plan_id, permission=READ)
    stmt = select(HeadcountSlot).where(HeadcountSlot.workforce_plan_id == plan.id)
    if status:
        stmt = stmt.where(HeadcountSlot.status == status)
    rows = list((await db.execute(stmt.order_by(HeadcountSlot.plan_line_id, HeadcountSlot.seq))).scalars())
    return await slots_out(db, plan, rows)


async def cancel_slot(ctx: RequestContext, db: AsyncSession, slot_id: uuid.UUID, reason: str) -> SlotOut:
    slot = (
        await db.execute(select(HeadcountSlot).where(HeadcountSlot.id == slot_id).with_for_update())
    ).scalar_one_or_none()
    if slot is None:
        raise NotFound("Headcount slot not found")
    plan = await get_plan(ctx, db, slot.workforce_plan_id, permission=WRITE)
    if slot.status != "open":
        raise conflict("invalid_transition", f"A {slot.status} slot cannot be cancelled", status=slot.status)
    if slot.demand_id is not None:
        raise conflict(
            "slot_has_demand", "Cancel or close the slot's Demand first", demand_id=str(slot.demand_id)
        )
    cancel(slot, reason)
    await db.flush()
    audit.record(
        "workforce.slot_cancelled", "headcount_slot", slot.id, session=db, workforce_plan_id=str(plan.id)
    )
    return (await slots_out(db, plan, [slot]))[0]


# ================================================================ event handlers (system context)
async def _slot_by_demand(db: AsyncSession, demand_id: uuid.UUID) -> HeadcountSlot | None:
    return (
        await db.execute(select(HeadcountSlot).where(HeadcountSlot.demand_id == demand_id).with_for_update())
    ).scalar_one_or_none()


async def on_demand_opened(db: AsyncSession, demand_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.demands import Demand

    slot = await _slot_by_demand(db, demand_id)
    if slot is None:
        demand = await db.get(Demand, demand_id)
        line_id = getattr(demand, "workforce_plan_line_id", None) if demand else None
        if line_id is None:
            return None
        line = await db.get(WorkforcePlanLine, line_id)
        if line is None:
            return None
        slot = (
            await db.execute(
                select(HeadcountSlot)
                .where(
                    HeadcountSlot.plan_line_id == (line.lineage_id or line.id),
                    HeadcountSlot.status == "open",
                    HeadcountSlot.demand_id.is_(None),
                )
                .order_by(HeadcountSlot.seq)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if slot is None:
            return None
        slot.demand_id = demand_id
        slot.converted_at = slot.converted_at or now()
        audit.record(
            "workforce.slot_claimed", "headcount_slot", slot.id, session=db, demand_id=str(demand_id)
        )
    if slot.status != "open":
        return slot.id
    slot.status = "requisitioned"
    slot.requisitioned_at = now()
    await db.flush()
    plan = await db.get(WorkforcePlan, slot.workforce_plan_id)
    if plan is not None:
        await refresh_line_status(db, plan, slot.plan_line_id)
    await db.flush()
    return slot.id


async def on_placement_activated(db: AsyncSession, placement_id: uuid.UUID) -> uuid.UUID | None:
    from app.events.workforce import WorkforceSlotFilled
    from app.models.placements import Placement

    placement = await db.get(Placement, placement_id)
    if placement is None or placement.demand_id is None:
        return None
    slot = await _slot_by_demand(db, placement.demand_id)
    if slot is None or slot.status not in ("open", "requisitioned"):
        return None
    slot.status = "filled"
    slot.placement_id = placement.id
    slot.filled_at = now()
    slot.requisitioned_at = slot.requisitioned_at or slot.filled_at
    await db.flush()
    plan = await db.get(WorkforcePlan, slot.workforce_plan_id)
    if plan is not None:
        await refresh_line_status(db, plan, slot.plan_line_id)
        emit(
            db,
            WorkforceSlotFilled(
                tenant_id=slot.tenant_id,
                workforce_plan_id=plan.id,
                slot_id=slot.id,
                placement_id=placement.id,
            ),
        )
    await db.flush()
    return slot.id


async def on_demand_status_changed(
    db: AsyncSession, demand_id: uuid.UUID, to_status: str
) -> uuid.UUID | None:
    if to_status not in ("cancelled", "closed"):
        return None
    slot = await _slot_by_demand(db, demand_id)
    if slot is None or slot.status not in ("open", "requisitioned") or slot.placement_id is not None:
        return None
    slot.status = "open"
    slot.demand_id = None
    slot.converted_at = None
    slot.requisitioned_at = None
    await db.flush()
    audit.record(
        "workforce.slot_released",
        "headcount_slot",
        slot.id,
        session=db,
        demand_id=str(demand_id),
        demand_status=to_status,
    )
    plan = await db.get(WorkforcePlan, slot.workforce_plan_id)
    if plan is not None:
        await refresh_line_status(db, plan, slot.plan_line_id)
    await db.flush()
    return slot.id
