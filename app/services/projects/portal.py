"""Client Portal project views (Epic 45, FR-231 / FR-80 / AD-26).

The session is a client session (org RLS on ``client_id``): another Client's project or deliverable is 404.
Restricted Client Users see only projects with an assignment of one of their scoped Placements
(``core.client_scope.visible_placement_ids``). Draft projects are not shown. No cost, rate, budget, burn,
vendor or internal note ever leaves this module (``schemas/projects_portal.py``).
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import visible_placement_ids
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.money import MoneyOut
from app.models.candidates import Candidate
from app.models.commercials import ContractMilestone
from app.models.projects import Deliverable, Project, ProjectAssignment, ProjectRole
from app.schemas.projects_portal import (
    DeliverablePortalOut,
    MilestonePortalOut,
    ProjectPortalDetail,
    ProjectPortalOut,
    RolePortalOut,
)
from app.services.client_portal_base import display_name
from app.services.projects import deliverables

VISIBLE_STATUSES = ("active", "on_hold", "completed")
_ORDER = {"submitted": 0, "in_progress": 1, "planned": 2, "accepted": 3}


async def _visible_stmt(ctx: RequestContext, db: AsyncSession) -> Any:
    stmt = select(Project).where(Project.status.in_(VISIBLE_STATUSES))
    scope = await visible_placement_ids(ctx, db)
    if scope is not None:
        stmt = stmt.where(
            Project.id.in_(
                select(ProjectAssignment.project_id).where(
                    ProjectAssignment.placement_id.in_(scope or [uuid.uuid4()])
                )
            )
        )
    return stmt


async def _project(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> Project:
    row = (
        await db.execute((await _visible_stmt(ctx, db)).where(Project.id == project_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Project not found")
    return row


def _deliverable_out(d: Deliverable) -> DeliverablePortalOut:
    return DeliverablePortalOut(
        id=d.id,
        project_id=d.project_id,
        milestone_id=d.milestone_id,
        name=d.name,
        description=d.description,
        acceptance_criteria=d.acceptance_criteria,
        due_date=d.due_date,
        status=d.status,
        submitted_at=d.submitted_at,
        accepted_at=d.accepted_at,
        last_rejection_reason=d.last_rejection_reason,
    )


async def client_list(ctx: RequestContext, db: AsyncSession) -> list[ProjectPortalOut]:
    rows = list(
        (
            await db.execute((await _visible_stmt(ctx, db)).order_by(Project.start_date.desc().nulls_last()))
        ).scalars()
    )
    if not rows:
        return []
    waiting = dict(
        (
            await db.execute(
                select(Deliverable.project_id, func.count(Deliverable.id))
                .where(Deliverable.project_id.in_([r.id for r in rows]), Deliverable.status == "submitted")
                .group_by(Deliverable.project_id)
            )
        ).tuples().all()
    )
    return [
        ProjectPortalOut(
            id=r.id,
            human_id=r.human_id,
            name=r.name,
            status=r.status,
            start_date=r.start_date,
            end_date=r.end_date,
            awaiting_acceptance=int(waiting.get(r.id, 0)),
        )
        for r in rows
    ]


async def _billed(
    tenant_id: uuid.UUID, milestone_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, str | None]]:
    """Milestone id -> (invoice id, number) of the issued invoice billing it. The posting ledger is internal
    (org RLS), so it is read in an internal session for milestone ids the client session already returned."""
    from app.core.database import tenant_session
    from app.models.billing import Invoice
    from app.models.postings import BillingPosting

    if not milestone_ids:
        return {}
    async with tenant_session(tenant_id) as idb:
        rows = await idb.execute(
            select(BillingPosting.source_id, Invoice.id, Invoice.number)
            .join(Invoice, Invoice.id == BillingPosting.document_id)
            .where(
                BillingPosting.source_kind == "milestone",
                BillingPosting.source_id.in_(list(milestone_ids)),
                BillingPosting.state != "released",
                Invoice.status == "issued",
            )
        )
        return {mid: (iid, number) for mid, iid, number in rows.all()}


async def client_detail(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> ProjectPortalDetail:
    project = await _project(ctx, db, project_id)
    scope = await visible_placement_ids(ctx, db)
    role_rows = list(
        (
            await db.execute(
                select(ProjectRole)
                .where(ProjectRole.project_id == project.id, ProjectRole.status != "cancelled")
                .order_by(ProjectRole.start_date.nulls_last(), ProjectRole.title)
            )
        ).scalars()
    )
    active = list(
        (
            await db.execute(
                select(ProjectAssignment).where(
                    ProjectAssignment.project_id == project.id, ProjectAssignment.status == "active"
                )
            )
        ).scalars()
    )
    cids = {a.candidate_id for a in active if a.candidate_id and (scope is None or a.placement_id in scope)}
    names = (
        dict(
            (await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cids)))).tuples().all()
        )
        if cids
        else {}
    )
    workers: dict[uuid.UUID, list[str]] = defaultdict(list)
    for a in active:
        if a.candidate_id in names:
            workers[a.project_role_id].append(display_name(names[a.candidate_id]))
    milestones: list[MilestonePortalOut] = []
    if project.contract_id is not None:
        ms = list(
            (
                await db.execute(
                    select(ContractMilestone)
                    .where(ContractMilestone.contract_id == project.contract_id)
                    .order_by(ContractMilestone.due_date.nulls_last(), ContractMilestone.created_at)
                )
            ).scalars()
        )
        tenant_id = getattr(ctx.principal, "tenant_id", None) or ctx.tenant_id
        billed = await _billed(tenant_id, [m.id for m in ms]) if tenant_id else {}
        milestones = [
            MilestonePortalOut(
                id=m.id,
                name=m.name,
                due_date=m.due_date,
                amount=MoneyOut(amount=m.amount, currency=m.currency),
                status=m.status,
                billed=m.id in billed,
                invoice_id=billed[m.id][0] if m.id in billed else None,
                invoice_number=billed[m.id][1] if m.id in billed else None,
            )
            for m in ms
            if m.status != "cancelled"
        ]
    ds = list(
        (
            await db.execute(
                select(Deliverable)
                .where(Deliverable.project_id == project.id, Deliverable.status != "cancelled")
                .order_by(Deliverable.due_date.nulls_last(), Deliverable.created_at)
            )
        ).scalars()
    )
    ds.sort(key=lambda d: _ORDER.get(d.status, 9))
    return ProjectPortalDetail(
        id=project.id,
        human_id=project.human_id,
        name=project.name,
        status=project.status,
        start_date=project.start_date,
        end_date=project.end_date,
        awaiting_acceptance=sum(1 for d in ds if d.status == "submitted"),
        roles=[
            RolePortalOut(
                id=r.id,
                title=r.title,
                headcount=r.headcount,
                status=r.status,
                start_date=r.start_date,
                end_date=r.end_date,
                workers=sorted(workers.get(r.id, [])),
            )
            for r in role_rows
        ],
        milestones=milestones,
        deliverables=[_deliverable_out(d) for d in ds],
    )


async def client_decide(
    ctx: RequestContext, db: AsyncSession, deliverable_id: uuid.UUID, outcome: str, reason: str | None = None
) -> DeliverablePortalOut:
    """Accept / reject a deliverable of a visible project (★ actor = the Client User)."""
    project_id = (
        await db.execute(select(Deliverable.project_id).where(Deliverable.id == deliverable_id))
    ).scalar_one_or_none()
    if project_id is None:
        raise NotFound("Deliverable not found")
    try:
        await _project(ctx, db, project_id)
    except NotFound:
        raise NotFound("Deliverable not found") from None
    row = await deliverables.portal_decide(ctx, db, deliverable_id, outcome, reason)
    await db.refresh(row)
    return _deliverable_out(row)
