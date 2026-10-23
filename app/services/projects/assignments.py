# ruff: file-ignore[line-too-long]
"""Project assignments (Epic 45, FR-229): Placements staffed on project roles.

Starting an assignment links the Placement through ``placements_service.link_project`` (the only writer of the
placement link columns); ending the last one unlinks it. One active assignment per Placement (409
``placement_already_assigned``; the link columns are scalar). Ending -- by a User or because the Placement ended
or was dropped -- emits ``ProjectAssignmentEnded`` exactly once (guarded by the locked ``active -> ended`` change).
"""

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.projects import ProjectAssignmentEnded
from app.models.candidates import Candidate
from app.models.placements import Placement
from app.models.projects import Project, ProjectAssignment, ProjectRole
from app.schemas.projects import AssignmentIn, AssignmentOut
from app.services.projects import roles
from app.services.projects.common import (
    READ,
    WRITE,
    conflict,
    get_project,
    invalid,
    scoped,
    tenant_of,
    timeline,
)

ASSIGNABLE = frozenset({"pre_joining", "joining_confirmed", "active"})
CLOSED_PLACEMENT = frozenset({"ended", "dropped"})
OPEN_PROJECT = frozenset({"draft", "active", "on_hold"})


async def outs(db: AsyncSession, rows: Sequence[ProjectAssignment]) -> list[AssignmentOut]:
    if not rows:
        return []
    role_titles = dict(
        (
            await db.execute(
                select(ProjectRole.id, ProjectRole.title).where(
                    ProjectRole.id.in_({r.project_role_id for r in rows})
                )
            )
        ).all()
    )
    placements = dict(
        (
            await db.execute(
                select(Placement.id, Placement.human_id).where(
                    Placement.id.in_({r.placement_id for r in rows})
                )
            )
        ).all()
    )
    cids = {r.candidate_id for r in rows if r.candidate_id}
    names = (
        dict(
            (await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cids)))).all()
        )
        if cids
        else {}
    )
    return [
        AssignmentOut(
            id=r.id,
            project_id=r.project_id,
            project_role_id=r.project_role_id,
            role_title=role_titles.get(r.project_role_id),
            placement_id=r.placement_id,
            placement_human_id=placements.get(r.placement_id),
            candidate_id=r.candidate_id,
            worker_name=names.get(r.candidate_id) if r.candidate_id else None,
            allocation_pct=r.allocation_pct,
            start_date=r.start_date,
            end_date=r.end_date,
            status=r.status,  # type: ignore[arg-type]
            ended_reason=r.ended_reason,
        )
        for r in rows
    ]


async def list_for_role(ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID) -> list[AssignmentOut]:
    role, _ = await roles.get_role(ctx, db, role_id)
    rows = list(
        (
            await db.execute(
                select(ProjectAssignment)
                .where(ProjectAssignment.project_role_id == role.id)
                .order_by(ProjectAssignment.start_date, ProjectAssignment.created_at)
            )
        ).scalars()
    )
    return await outs(db, rows)


async def list_for_project(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID
) -> list[AssignmentOut]:
    await get_project(ctx, db, project_id)
    rows = list(
        (
            await db.execute(
                select(ProjectAssignment)
                .where(ProjectAssignment.project_id == project_id)
                .order_by(ProjectAssignment.status, ProjectAssignment.start_date)
            )
        ).scalars()
    )
    return await outs(db, rows)


# ================================================================ start
async def start(
    ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID, data: AssignmentIn
) -> AssignmentOut:
    from app.services import placements_service

    role, project = await roles.get_role(ctx, db, role_id, permission=WRITE, lock=True)
    if project.status not in OPEN_PROJECT:
        raise conflict(
            "project_closed", "Placements are assigned to open projects only", status=project.status
        )
    if role.status in ("closed", "cancelled"):
        raise conflict("role_closed", "The role is closed", status=role.status)
    placement = (
        await db.execute(select(Placement).where(Placement.id == data.placement_id).with_for_update())
    ).scalar_one_or_none()
    if placement is None:
        raise NotFound("Placement not found")
    if placement.client_id != project.client_id:
        raise invalid(
            "placement_client_mismatch", "The placement belongs to another client", field="placement_id"
        )
    if placement.status in CLOSED_PLACEMENT:
        raise conflict("placement_closed", "An ended or dropped placement cannot be assigned")
    if placement.status not in ASSIGNABLE:
        raise conflict("placement_not_assignable", "The placement is offboarding", status=placement.status)
    if data.end_date is not None and data.end_date < data.start_date:
        raise invalid(
            "assignment_dates_invalid", "The end date must be on or after the start date", field="end_date"
        )
    busy = (
        await db.execute(
            select(ProjectAssignment.id).where(
                ProjectAssignment.placement_id == placement.id, ProjectAssignment.status == "active"
            )
        )
    ).first()
    if busy is not None:
        raise conflict(
            "placement_already_assigned",
            "The placement already has an active project assignment",
            assignment_id=str(busy[0]),
        )
    warnings: list[str] = []
    if (role.start_date and data.start_date < role.start_date) or (
        role.end_date and data.end_date is not None and data.end_date > role.end_date
    ):
        warnings.append("assignment_outside_role_dates")
    row = ProjectAssignment(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        project_id=project.id,
        project_role_id=role.id,
        placement_id=placement.id,
        candidate_id=placement.candidate_id,
        allocation_pct=data.allocation_pct,
        start_date=data.start_date,
        end_date=data.end_date,
        status="active",
    )
    db.add(row)
    await db.flush()
    await placements_service.link_project(
        ctx, db, placement.id, project_id=project.id, project_role_id=role.id
    )
    await roles.refresh_status(db, role)
    await timeline(
        ctx,
        db,
        project.id,
        f"{project.human_id}: {placement.human_id} assigned to {role.title} ({row.allocation_pct}%)",
        {"assignment_id": str(row.id), "placement_id": str(placement.id)},
    )
    audit.record(
        "project_assignment.started",
        "project_assignment",
        row.id,
        session=db,
        project_id=str(project.id),
        placement_id=str(placement.id),
        allocation_pct=str(row.allocation_pct),
    )
    await db.refresh(row)
    (result,) = await outs(db, [row])
    return result.model_copy(update={"warnings": warnings})


# ================================================================ end
async def _end_row(
    ctx: RequestContext,
    db: AsyncSession,
    row: ProjectAssignment,
    end_date: date,
    reason: str | None,
    *,
    unlink: bool,
) -> bool:
    """``active -> ended`` on a locked row; returns False when it was already ended (no event)."""
    from app.services import placements_service

    if row.status != "active":
        return False
    row.status = "ended"
    row.end_date = max(end_date, row.start_date)
    row.ended_reason = (reason or None) and reason[:500]
    await db.flush()
    placement = await db.get(Placement, row.placement_id)
    if unlink and placement is not None and placement.status not in CLOSED_PLACEMENT:
        await placements_service.link_project(
            ctx, db, row.placement_id, project_id=None, project_role_id=None
        )
    role = await db.get(ProjectRole, row.project_role_id)
    if role is not None:
        await roles.refresh_status(db, role)
    project = await db.get(Project, row.project_id)
    label = project.human_id if project else "Project"
    await timeline(
        ctx,
        db,
        row.project_id,
        f"{label}: assignment ended on {row.end_date.isoformat()}",
        {"assignment_id": str(row.id), "reason": row.ended_reason},
    )
    audit.record(
        "project_assignment.ended",
        "project_assignment",
        row.id,
        session=db,
        project_id=str(row.project_id),
        end_date=row.end_date.isoformat(),
        reason=row.ended_reason,
    )
    candidate_id = row.candidate_id or (placement.candidate_id if placement else None)
    if candidate_id is not None:
        emit(
            db,
            ProjectAssignmentEnded(
                tenant_id=row.tenant_id,
                assignment_id=row.id,
                project_id=row.project_id,
                project_role_id=row.project_role_id,
                placement_id=row.placement_id,
                candidate_id=candidate_id,
                ended_on=row.end_date,
            ),
        )
    return True


async def end(
    ctx: RequestContext, db: AsyncSession, assignment_id: uuid.UUID, end_date: date, reason: str | None
) -> AssignmentOut:
    found = (
        await db.execute(
            scoped(
                ctx,
                select(ProjectAssignment).join(Project, Project.id == ProjectAssignment.project_id),
                WRITE,
            )
            .where(ProjectAssignment.id == assignment_id)
            .with_for_update(of=ProjectAssignment)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFound("Assignment not found")
    if found.status == "active" and end_date < found.start_date:
        raise invalid(
            "assignment_dates_invalid", "The end date must be on or after the start date", field="end_date"
        )
    await _end_row(ctx, db, found, end_date, reason, unlink=True)
    await db.refresh(found)
    return (await outs(db, [found]))[0]


async def end_for_placement(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, end_date: date, reason: str | None
) -> int:
    """``PlacementEnded`` / ``PlacementDropped``: end every active assignment of the Placement (once)."""
    rows = list(
        (
            await db.execute(
                select(ProjectAssignment)
                .where(ProjectAssignment.placement_id == placement_id, ProjectAssignment.status == "active")
                .with_for_update()
            )
        ).scalars()
    )
    ended = 0
    for row in rows:
        if await _end_row(ctx, db, row, end_date, reason, unlink=False):
            ended += 1
    return ended


async def get_assignment(ctx: RequestContext, db: AsyncSession, assignment_id: uuid.UUID) -> AssignmentOut:
    found = (
        await db.execute(
            scoped(
                ctx, select(ProjectAssignment).join(Project, Project.id == ProjectAssignment.project_id), READ
            ).where(ProjectAssignment.id == assignment_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFound("Assignment not found")
    return (await outs(db, [found]))[0]
