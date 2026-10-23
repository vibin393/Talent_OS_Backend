"""SOW milestones (Epic 16 A4, FR-89 / FR-147 input). ``billed`` is derived from ``billing_postings`` (state
<> released), never written; the ``milestone`` posting source loader lives in ``hooks.py``."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, require_human
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.money import MoneyOut, quantize_amount
from app.models.commercials import ContractMilestone
from app.models.postings import BillingPosting
from app.schemas.commercials import MilestoneIn, MilestoneOut, MilestoneUpdate
from app.services.commercials import contracts
from app.services.commercials.common import conflict, now, tenant_of, timeline, user_id

SOURCE_KIND = "milestone"


async def billed_ids(db: AsyncSession, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    rows = await db.execute(
        select(BillingPosting.source_id).where(
            BillingPosting.source_kind == SOURCE_KIND,
            BillingPosting.source_id.in_(list(ids)),
            BillingPosting.state != "released",
        )
    )
    return {r for (r,) in rows.all()}


def _out(row: ContractMilestone, billed: bool) -> MilestoneOut:
    return MilestoneOut(
        id=row.id,
        contract_id=row.contract_id,
        name=row.name,
        amount=MoneyOut(amount=row.amount, currency=row.currency),
        due_date=row.due_date,
        status=row.status,
        accepted_at=row.accepted_at,
        accepted_by=row.accepted_by,
        billed=billed,
    )


async def list_milestones(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    contract_id: uuid.UUID | None = None,
    *,
    project_id: uuid.UUID | None = None,
) -> list[MilestoneOut]:
    """List the milestones of a SOW; Epic 45: ``project_id`` narrows to the project's SOW (joins
    ``projects.contract_id``; a project without a SOW has none)."""
    stmt = select(ContractMilestone)
    if contract_id is not None:
        await contracts.get_row(db, contract_id)
        stmt = stmt.where(ContractMilestone.contract_id == contract_id)
    if project_id is not None:
        from app.models.projects import Project

        stmt = stmt.join(Project, Project.contract_id == ContractMilestone.contract_id).where(
            Project.id == project_id
        )
    elif contract_id is None:
        raise NotFound("Contract not found")
    rows = list(
        (
            await db.execute(
                stmt.order_by(ContractMilestone.due_date.nulls_last(), ContractMilestone.created_at)
            )
        ).scalars()
    )
    billed = await billed_ids(db, [r.id for r in rows])
    return [_out(r, r.id in billed) for r in rows]


async def add_milestone(
    ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID, data: MilestoneIn
) -> MilestoneOut:
    contract = await contracts.get_row(db, contract_id, lock=True)
    if contract.contract_type != "sow":
        raise conflict("milestones_sow_only", "Milestones belong to SOWs")
    if contract.status in ("expired", "terminated"):
        raise conflict("contract_closed", "This contract is closed", status=contract.status)
    row = ContractMilestone(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        client_id=contract.client_id,
        contract_id=contract.id,
        name=data.name,
        amount=quantize_amount(data.amount, contract.currency),
        currency=contract.currency,
        due_date=data.due_date,
        status="planned",
    )
    db.add(row)
    await db.flush()
    await timeline(
        ctx,
        db,
        contracts.ENTITY,
        contract.id,
        f"{contract.human_id}: milestone added",
        {"milestone_id": str(row.id)},
    )
    await db.refresh(row)
    return _out(row, False)


async def _get(db: AsyncSession, milestone_id: uuid.UUID) -> ContractMilestone:
    row = (
        await db.execute(
            select(ContractMilestone).where(ContractMilestone.id == milestone_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Milestone not found")
    return row


async def update_milestone(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    milestone_id: uuid.UUID,
    data: MilestoneUpdate,
) -> MilestoneOut:
    row = await _get(db, milestone_id)
    if row.status != "planned":
        raise conflict("milestone_not_editable", "Only planned milestones can be edited", status=row.status)
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    if "amount" in changes:
        changes["amount"] = quantize_amount(changes["amount"], row.currency)
    for key, value in changes.items():
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return _out(row, False)


async def accept_milestone(ctx: RequestContext, db: AsyncSession, milestone_id: uuid.UUID) -> MilestoneOut:
    row = await _get(db, milestone_id)
    if row.status != "planned":
        raise conflict("milestone_not_planned", "Only planned milestones can be accepted", status=row.status)
    contract = await contracts.get_row(db, row.contract_id)
    if contract.status != "active":
        raise conflict(
            "contract_not_active", "Milestones are accepted on active SOWs only", status=contract.status
        )
    row.status = "accepted"
    row.accepted_at = now()
    row.accepted_by = user_id(ctx)
    await db.flush()
    await timeline(
        ctx,
        db,
        contracts.ENTITY,
        contract.id,
        f"{contract.human_id}: milestone accepted",
        {"milestone_id": str(row.id)},
    )
    await db.refresh(row)
    return _out(row, False)


async def cancel_milestone(ctx: RequestContext, db: AsyncSession, milestone_id: uuid.UUID) -> MilestoneOut:
    row = await _get(db, milestone_id)
    if row.status == "cancelled":
        return _out(row, False)
    if await billed_ids(db, [row.id]):
        raise conflict("milestone_posted", "This milestone is already on an invoice")
    row.status = "cancelled"
    await db.flush()
    await timeline(
        ctx, db, contracts.ENTITY, row.contract_id, "Milestone cancelled", {"milestone_id": str(row.id)}
    )
    await db.refresh(row)
    return _out(row, False)


async def mark_milestone_billable(
    ctx: RequestContext, db: AsyncSession, milestone_id: uuid.UUID, *, actor: ActorRef
) -> MilestoneOut:
    """Make a milestone billable (Epic 45; Epic 36 contract, AD-24 / FR-257).

    The guarded, actor-carrying ``planned -> accepted`` transition (the ``milestone`` posting source
    bills accepted milestones of Active SOWs). Human actors only (403 ``human_only_action``); the contract
    must be an Active SOW (409 ``contract_not_active``); idempotent when already accepted; 409
    ``milestone_not_planned`` when cancelled. Records ``accepted_by`` (the internal User for ``user`` /
    ``recorded_by_user``), the timeline entry and an explicit audit event.
    """
    from app.core import audit

    require_human(actor)
    row = await _get(db, milestone_id)
    contract = await contracts.get_row(db, row.contract_id)
    if contract.contract_type != "sow" or contract.status != "active":
        raise conflict(
            "contract_not_active", "Milestones are accepted on active SOWs only", status=contract.status
        )
    if row.status == "accepted":
        return _out(row, bool(await billed_ids(db, [row.id])))
    if row.status != "planned":
        raise conflict("milestone_not_planned", "Only planned milestones can be accepted", status=row.status)
    row.status = "accepted"
    row.accepted_at = now()
    row.accepted_by = actor.id if actor.kind in ("user", "recorded_by_user") else None
    await db.flush()
    await timeline(
        ctx,
        db,
        contracts.ENTITY,
        contract.id,
        f"{contract.human_id}: milestone accepted (billable)",
        {"milestone_id": str(row.id), "actor": actor.to_json()},
    )
    audit.record(
        "milestone.billable",
        "contract_milestone",
        row.id,
        session=db,
        contract_id=str(contract.id),
        actor=actor.to_json(),
    )
    await db.refresh(row)
    return _out(row, False)
