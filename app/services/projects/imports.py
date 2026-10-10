# ruff: noqa: E501
"""Go-live importers of the ``projects`` domain (Epic 45; foundation 0.7, FR-263).

``import_project`` (SOW by contract human id, client by name, owner by email), ``import_project_role`` (project
by human id) and ``import_project_assignment`` (Placement by human id, role by title). Field names are the CSV
column names; a problem raises ``ValidationFailed`` naming the column. ``validate_only`` checks without writing
and returns ``None``.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.sequences import next_human_id
from app.models.clients import Client
from app.models.commercials import Contract
from app.models.identity import User
from app.models.placements import Placement
from app.models.projects import Project, ProjectAssignment, ProjectRole
from app.schemas.projects import AssignmentIn, ProjectAssignmentImportIn, ProjectImportIn, ProjectRoleImportIn
from app.services.projects import assignments
from app.services.projects.common import tenant_of, timeline


def _bad(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(
        message, code="import_invalid", details={"fields": [{"field": field, "message": message}]}
    )


async def _project(db: AsyncSession, human_id: str) -> Project:
    row = (await db.execute(select(Project).where(Project.human_id == human_id))).scalar_one_or_none()
    if row is None:
        raise _bad("project_human_id", "Unknown project")
    return row


async def import_project(
    ctx: RequestContext, db: AsyncSession, data: ProjectImportIn, *, validate_only: bool = False
) -> uuid.UUID | None:
    clients = list((await db.execute(select(Client).where(Client.name == data.client_name))).scalars())
    if len(clients) != 1:
        raise _bad("client_name", "Unknown client" if not clients else "Several clients have this name")
    client = clients[0]
    sow: Contract | None = None
    if data.sow_human_id:
        sow = (
            await db.execute(select(Contract).where(Contract.human_id == data.sow_human_id))
        ).scalar_one_or_none()
        if sow is None or sow.contract_type != "sow" or sow.client_id != client.id:
            raise _bad("sow_human_id", "Unknown SOW of this client")
    owner_id = None
    if data.owner_email:
        owner_id = (
            await db.execute(select(User.id).where(func.lower(User.email) == data.owner_email.lower()))
        ).scalar_one_or_none()
        if owner_id is None:
            raise _bad("owner_email", "No User has this email")
    if data.status != "draft" and (sow is None or sow.status != "active"):
        raise _bad("status", "Only projects under an Active SOW are imported as non-draft")
    if data.start_date and data.end_date and data.end_date < data.start_date:
        raise _bad("end_date", "The end date must be on or after the start date")
    if validate_only:
        return None
    row = Project(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, "project", "PRJ"),
        name=data.name,
        client_id=client.id,
        contract_id=sow.id if sow else None,
        legal_entity_id=sow.legal_entity_id if sow else None,
        currency=sow.currency if sow else None,
        owner_id=owner_id,
        start_date=data.start_date or (sow.start_date if sow else None),
        end_date=data.end_date or (sow.end_date if sow else None),
        description=data.description,
        status=data.status,
    )
    db.add(row)
    await db.flush()
    await timeline(ctx, db, row.id, f"{row.human_id}: imported", {"source": "golive"})
    return row.id


async def import_project_role(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    data: ProjectRoleImportIn,
    *,
    validate_only: bool = False,
) -> uuid.UUID | None:
    project = await _project(db, data.project_human_id)
    if project.status in ("completed", "cancelled"):
        raise _bad("project_human_id", "The project is closed")
    if data.start_date and data.end_date and data.end_date < data.start_date:
        raise _bad("end_date", "The end date must be on or after the start date")
    if validate_only:
        return None
    row = ProjectRole(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        project_id=project.id,
        title=data.title,
        headcount=data.headcount,
        start_date=data.start_date or project.start_date,
        end_date=data.end_date or project.end_date,
        status="open",
    )
    db.add(row)
    await db.flush()
    return row.id


async def import_project_assignment(
    ctx: RequestContext, db: AsyncSession, data: ProjectAssignmentImportIn, *, validate_only: bool = False
) -> uuid.UUID | None:
    project = await _project(db, data.project_human_id)
    roles = list(
        (
            await db.execute(
                select(ProjectRole).where(
                    ProjectRole.project_id == project.id, ProjectRole.title == data.role_title
                )
            )
        ).scalars()
    )
    if len(roles) != 1:
        raise _bad(
            "role_title", "Unknown role of this project" if not roles else "Several roles have this title"
        )
    placement = (
        await db.execute(select(Placement).where(Placement.human_id == data.placement_human_id))
    ).scalar_one_or_none()
    if placement is None:
        raise _bad("placement_human_id", "Unknown placement")
    if placement.client_id != project.client_id:
        raise _bad("placement_human_id", "The placement belongs to another client")
    if placement.status not in assignments.ASSIGNABLE:
        raise _bad("placement_human_id", "The placement is not active")
    busy = (
        await db.execute(
            select(ProjectAssignment.id).where(
                ProjectAssignment.placement_id == placement.id, ProjectAssignment.status == "active"
            )
        )
    ).first()
    if busy is not None:
        raise _bad("placement_human_id", "The placement already has an active project assignment")
    if data.end_date and data.end_date < data.start_date:
        raise _bad("end_date", "The end date must be on or after the start date")
    if validate_only:
        return None
    out = await assignments.start(
        ctx,
        db,
        roles[0].id,
        AssignmentIn(
            placement_id=placement.id,
            allocation_pct=data.allocation_pct,
            start_date=data.start_date,
            end_date=data.end_date,
        ),
    )
    return out.id
