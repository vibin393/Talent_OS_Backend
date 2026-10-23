# ruff: file-ignore[line-too-long]
"""Projects contract (Epic 45; Epic 36 stub, AD-39) and the domain's public functions.

``project_context`` keeps the FINAL signature of the foundation stub (``tests/contracts/signatures.json``);
``filled_count`` is the number of active assignments of the role. ``quick_search`` feeds the Epic 52 global
search (type ``project``); ``import_project`` / ``import_project_role`` / ``import_project_assignment`` are the
go-live importers (foundation 0.7).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.projects import ProjectRole
from app.schemas.projects import ProjectContext, ProjectRoleRef
from app.schemas.search import QuickHit
from app.services.projects import projects, roles
from app.services.projects.common import get_project
from app.services.projects.imports import import_project, import_project_assignment, import_project_role

__all__ = [
    "import_project",
    "import_project_assignment",
    "import_project_role",
    "project_context",
    "quick_search",
]


async def project_context(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> ProjectContext:
    project = await get_project(ctx, db, project_id)
    rows = list(
        (
            await db.execute(
                select(ProjectRole)
                .where(ProjectRole.project_id == project.id)
                .order_by(ProjectRole.start_date.nulls_last(), ProjectRole.created_at)
            )
        ).scalars()
    )
    fte = await roles.active_fte(db, [r.id for r in rows])
    return ProjectContext(
        project_id=project.id,
        human_id=project.human_id,
        name=project.name,
        client_id=project.client_id,
        contract_id=project.contract_id,
        status=project.status,
        start_date=project.start_date,
        end_date=project.end_date,
        roles=tuple(
            ProjectRoleRef(
                id=r.id,
                title=r.title,
                headcount=r.headcount,
                status=r.status,
                start_date=r.start_date,
                end_date=r.end_date,
                filled_count=fte.get(r.id, (0, 0))[1],
            )
            for r in rows
        ),
    )


async def quick_search(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[QuickHit]:
    rows = await projects.quick_hits(ctx, db, q, limit)
    term = (q or "").strip().lower()
    return [
        QuickHit(
            id=r.id,
            type="project",
            title=r.name,
            subtitle=r.status.replace("_", " ").capitalize(),
            human_id=r.human_id,
            url=f"/projects/{r.id}",
            exact=r.human_id.lower() == term,
        )
        for r in rows
    ]
