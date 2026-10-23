"""Project roles (Epic 45, FR-229): staffing slots of a project, their skills and the Demands raised for them.

``skills`` rows are ``{skill_key, label, min_proficiency}`` with ``skill_key = SkillKey.as_str()``, validated
through the P1 skill catalogue (``candidates.skills.get_ref``). Role status is derived on assignment changes
(``filled`` when active FTE >= headcount, else ``open``); ``closed`` / ``cancelled`` are set by a User.
Demands are created only through ``demands_service.create_draft(..., project_role_id=)`` and copy the role's
skills (coordinator decision 4: Epic 37 reads project evidence from them).
"""

import math
import uuid
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.skill_refs import SkillKey
from app.models.demands import Demand
from app.models.projects import Project, ProjectAssignment, ProjectRole
from app.schemas.projects import ProjectRoleIn, ProjectRoleOut, ProjectRoleUpdate, RoleSkill
from app.services.projects.common import (
    READ,
    WRITE,
    can_view_rates,
    conflict,
    get_project,
    invalid,
    scoped,
    tenant_of,
    timeline,
)

HUNDRED = Decimal(100)
OPEN_PROJECT = frozenset({"draft", "active", "on_hold"})


# ================================================================ skills
async def validate_skills(db: AsyncSession, skills: Sequence[RoleSkill]) -> list[dict[str, Any]]:
    from app.schemas.candidates import SkillRefIn
    from app.services.candidates.skills import get_ref

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, s in enumerate(skills):
        try:
            key = SkillKey.parse(s.skill_key)
        except ValueError:
            raise invalid("skill_invalid", "Unknown skill", field=f"skills.{i}.skill_key") from None
        ref = await get_ref(db, SkillRefIn(kind=key.kind, id=key.id))
        if ref is None:
            raise invalid("skill_invalid", "Unknown skill", field=f"skills.{i}.skill_key")
        if key.as_str() in seen:
            continue
        seen.add(key.as_str())
        out.append(
            {"skill_key": key.as_str(), "label": s.label or ref.name, "min_proficiency": s.min_proficiency}
        )
    return out


# ================================================================ reads
async def active_fte(db: AsyncSession, role_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[Decimal, int]]:
    """Role id -> (sum of active allocations / 100, number of active assignments)."""
    if not role_ids:
        return {}
    rows = await db.execute(
        select(
            ProjectAssignment.project_role_id,
            func.coalesce(func.sum(ProjectAssignment.allocation_pct), 0),
            func.count(ProjectAssignment.id),
        )
        .where(ProjectAssignment.project_role_id.in_(list(role_ids)), ProjectAssignment.status == "active")
        .group_by(ProjectAssignment.project_role_id)
    )
    return {rid: (Decimal(total) / HUNDRED, int(n)) for rid, total, n in rows.all()}


async def outs(ctx: RequestContext, db: AsyncSession, rows: Sequence[ProjectRole]) -> list[ProjectRoleOut]:
    ids = [r.id for r in rows]
    fte = await active_fte(db, ids)
    demands: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    if ids:
        for did, rid in (
            await db.execute(select(Demand.id, Demand.project_role_id).where(Demand.project_role_id.in_(ids)))
        ).all():
            demands[rid].append(did)
    rates = can_view_rates(ctx)
    return [
        ProjectRoleOut(
            id=r.id,
            project_id=r.project_id,
            title=r.title,
            headcount=r.headcount,
            status=r.status,
            start_date=r.start_date,
            end_date=r.end_date,
            skills=[RoleSkill(**s) for s in (r.skills or [])],
            bill_rate=r.bill_rate if rates else None,
            bill_rate_unit=r.bill_rate_unit,
            notes=r.notes,
            filled_fte=fte.get(r.id, (Decimal(0), 0))[0].quantize(Decimal("0.01")),
            active_assignments=fte.get(r.id, (Decimal(0), 0))[1],
            demand_ids=demands.get(r.id, []),
        )
        for r in rows
    ]


async def get_role(
    ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID, *, permission: str = READ, lock: bool = False
) -> tuple[ProjectRole, Project]:
    stmt = scoped(
        ctx, select(ProjectRole, Project).join(Project, Project.id == ProjectRole.project_id), permission
    ).where(ProjectRole.id == role_id)
    if lock:
        stmt = stmt.with_for_update(of=ProjectRole)
    found = (await db.execute(stmt)).first()
    if found is None:
        raise NotFound("Project role not found")
    return found[0], found[1]


async def list_roles(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> list[ProjectRoleOut]:
    await get_project(ctx, db, project_id)
    rows = list(
        (
            await db.execute(
                select(ProjectRole)
                .where(ProjectRole.project_id == project_id)
                .order_by(ProjectRole.start_date.nulls_last(), ProjectRole.created_at)
            )
        ).scalars()
    )
    return await outs(ctx, db, rows)


# ================================================================ writes
def _check_dates(start: Any, end: Any) -> None:
    if start is not None and end is not None and end < start:
        raise invalid(
            "role_dates_invalid", "The end date must be on or after the start date", field="end_date"
        )


async def create_role(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID, data: ProjectRoleIn
) -> ProjectRoleOut:
    project = await get_project(ctx, db, project_id, permission=WRITE)
    if project.status not in OPEN_PROJECT:
        raise conflict("project_closed", "Roles are added to open projects only", status=project.status)
    _check_dates(data.start_date, data.end_date)
    row = ProjectRole(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        project_id=project.id,
        title=data.title,
        headcount=data.headcount,
        start_date=data.start_date or project.start_date,
        end_date=data.end_date or project.end_date,
        skills=await validate_skills(db, data.skills),
        bill_rate=data.bill_rate,
        bill_rate_unit=data.bill_rate_unit,
        notes=data.notes,
        status="open",
    )
    db.add(row)
    await db.flush()
    await timeline(
        ctx, db, project.id, f"{project.human_id}: role {row.title} added", {"role_id": str(row.id)}
    )
    await db.refresh(row)
    return (await outs(ctx, db, [row]))[0]


async def update_role(
    ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID, data: ProjectRoleUpdate
) -> ProjectRoleOut:
    row, project = await get_role(ctx, db, role_id, permission=WRITE, lock=True)
    changes = data.model_dump(exclude_unset=True)
    if "skills" in changes:
        row.skills = await validate_skills(db, data.skills or [])
        changes.pop("skills")
    for required in ("title", "headcount"):
        if required in changes and changes[required] is None:
            raise invalid(f"{required}_required", f"The role needs its {required}", field=required)
    _check_dates(changes.get("start_date", row.start_date), changes.get("end_date", row.end_date))
    status = changes.pop("status", None)
    for key, value in changes.items():
        setattr(row, key, value)
    if status in ("closed", "cancelled"):
        row.status = status
    elif status == "open" or row.status in ("open", "filled"):
        row.status = "open"
        await db.flush()
        await refresh_status(db, row)
    await db.flush()
    await timeline(
        ctx,
        db,
        project.id,
        f"{project.human_id}: role {row.title} updated",
        {"role_id": str(row.id), "status": row.status},
    )
    await db.refresh(row)
    return (await outs(ctx, db, [row]))[0]


async def delete_role(ctx: RequestContext, db: AsyncSession, role_id: uuid.UUID) -> None:
    row, project = await get_role(ctx, db, role_id, permission=WRITE, lock=True)
    used = (
        await db.execute(
            select(ProjectAssignment.id).where(ProjectAssignment.project_role_id == row.id).limit(1)
        )
    ).first() or (
        await db.execute(select(Demand.id).where(Demand.project_role_id == row.id).limit(1))
    ).first()
    if used:
        raise conflict("role_in_use", "The role has assignments or demands; close or cancel it instead")
    await db.delete(row)
    await db.flush()
    await timeline(
        ctx, db, project.id, f"{project.human_id}: role {row.title} removed", {"role_id": str(row.id)}
    )


async def refresh_status(db: AsyncSession, role: ProjectRole) -> None:
    """Derive the status: ``filled`` when the active FTE covers the headcount, else ``open`` (User-set
    ``closed`` / ``cancelled`` stay)."""
    if role.status in ("closed", "cancelled"):
        return
    fte = (await active_fte(db, [role.id])).get(role.id, (Decimal(0), 0))[0]
    wanted = "filled" if fte >= Decimal(role.headcount) else "open"
    if role.status != wanted:
        role.status = wanted
        await db.flush()


# ================================================================ demands from roles
async def create_demand_for(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID, role_id: uuid.UUID, positions: int | None
) -> dict[str, Any]:
    """``POST /projects/{id}/roles/{role_id}/demand``: a draft Demand for the role's unfilled positions
    (``ceil(headcount - active FTE)`` unless given) carrying the role's skills."""
    from app.schemas.demands import DemandCreate, DemandSkillIn
    from app.services import demands_service

    role, project = await get_role(ctx, db, role_id, permission=WRITE)
    if project.id != project_id:
        raise NotFound("Project role not found")
    if project.status not in OPEN_PROJECT or role.status in ("closed", "cancelled"):
        raise conflict("role_closed", "Demands are raised for open roles of open projects only")
    if positions is None:
        fte = (await active_fte(db, [role.id])).get(role.id, (Decimal(0), 0))[0]
        positions = math.ceil(Decimal(role.headcount) - fte)
        if positions < 1:
            raise conflict("role_filled", "The role is already filled")
    with_min = "min_proficiency" in DemandSkillIn.model_fields
    skills = []
    for s in role.skills or []:
        key = SkillKey.parse(s["skill_key"])
        item: dict[str, Any] = {
            "label": s.get("label") or key.as_str(),
            "skill_id": key.id,
            "skill_ref_kind": key.kind,
        }
        if with_min and s.get("min_proficiency") is not None:
            item["min_proficiency"] = s["min_proficiency"]
        skills.append(DemandSkillIn(**item))
    data = DemandCreate(
        title=role.title,
        client_id=project.client_id,
        positions=positions,
        start_date=role.start_date,
        skills=skills or None,
    )
    demand = await demands_service.create_draft(ctx, db, data, project_role_id=role.id)
    await timeline(
        ctx,
        db,
        project.id,
        f"{project.human_id}: demand raised for role {role.title}",
        {"role_id": str(role.id), "demand_id": str(demand.get("id")), "positions": positions},
    )
    return demand
