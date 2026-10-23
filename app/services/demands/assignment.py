"""Recruiter assignment (FR-15, story 4.5).

``set_assignments`` replaces the active set of assignees (users and teams) of an Open / On Hold demand;
removed rows keep their history (``removed_at``). One active primary per demand, always a user. Hooks
``record_first_*`` let later epics stamp the first activity / application / submission per assignee.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.events.demands import DemandAssigned
from app.models.demands import DemandAssignment
from app.schemas.demands import AssignmentOut, AssignmentsIn, AssignmentsOut
from app.services import directory_service as directory
from app.services.demands import sla
from app.services.demands.common import ASSIGN, READ, acting_as, get_for, now, principal_of, timeline

ASSIGNABLE_STATUSES = frozenset({"open", "on_hold"})


async def _active(db: AsyncSession, demand_id: uuid.UUID) -> list[DemandAssignment]:
    rows = await db.execute(
        select(DemandAssignment)
        .where(DemandAssignment.demand_id == demand_id, DemandAssignment.removed_at.is_(None))
        .order_by(DemandAssignment.assigned_at, DemandAssignment.id)
    )
    return list(rows.scalars())


async def _out(db: AsyncSession, rows: list[DemandAssignment]) -> AssignmentsOut:
    users = await directory.user_names(db, [r.user_id for r in rows if r.user_id])
    teams = await directory.team_names(db, [r.team_id for r in rows if r.team_id])
    items = [
        AssignmentOut(
            id=r.id,
            user_id=r.user_id,
            team_id=r.team_id,
            name=users.get(r.user_id)
            if r.user_id
            else (str(teams.get(r.team_id)) if r.team_id in teams else None),
            is_primary=r.is_primary,
            assigned_by=r.assigned_by,
            assigned_at=r.assigned_at,
            first_activity_at=r.first_activity_at,
            first_application_at=r.first_application_at,
            first_submission_at=r.first_submission_at,
        )
        for r in rows
    ]
    items.sort(key=lambda a: (not a.is_primary, a.team_id is not None, (a.name or "").lower()))
    return AssignmentsOut(items=items)


async def list_assignments(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> AssignmentsOut:
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    return await _out(db, await _active(db, demand.id))


async def set_assignments(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: AssignmentsIn
) -> AssignmentsOut:
    principal = principal_of(ctx)
    demand = await get_for(ctx, db, demand_id, ASSIGN)
    if demand.status not in ASSIGNABLE_STATUSES:
        raise ConflictError("Only Open or On Hold demands can be assigned", code="demand_not_assignable")
    user_ids, team_ids = list(dict.fromkeys(data.user_ids)), list(dict.fromkeys(data.team_ids))
    if data.primary_user_id is not None and data.primary_user_id not in user_ids:
        raise ValidationFailed(
            "The primary recruiter must be one of the assigned users",
            code="primary_not_assigned",
            details={"fields": [{"field": "primary_user_id", "message": "not among user_ids"}]},
        )
    bad_users = set(user_ids) - await directory.active_user_ids(db, user_ids)
    bad_teams = set(team_ids) - await directory.existing_team_ids(db, team_ids)
    if bad_users or bad_teams:
        raise ValidationFailed(
            "Unknown or inactive assignee",
            code="invalid_reference",
            details={"user_ids": sorted(map(str, bad_users)), "team_ids": sorted(map(str, bad_teams))},
        )
    at = now()
    current = await _active(db, demand.id)
    by_key: dict[tuple[str, uuid.UUID | None], DemandAssignment] = {
        ("u", r.user_id) if r.user_id else ("t", r.team_id): r for r in current
    }
    wanted: set[tuple[str, uuid.UUID | None]] = {("u", u) for u in user_ids} | {("t", t) for t in team_ids}
    # 1. removals and primary reset first (the partial unique index allows one active primary).
    for key, row in by_key.items():
        if key not in wanted:
            row.removed_at = at
            row.is_primary = False
        elif row.is_primary and row.user_id != data.primary_user_id:
            row.is_primary = False
    await db.flush()
    added_users: list[uuid.UUID] = []
    added_teams: list[uuid.UUID] = []
    for kind, ident in sorted(wanted - set(by_key), key=lambda k: (k[0], str(k[1]))):
        row = DemandAssignment(
            id=uuid.uuid4(),
            tenant_id=demand.tenant_id,
            demand_id=demand.id,
            user_id=ident if kind == "u" else None,
            team_id=ident if kind == "t" else None,
            is_primary=False,
            assigned_by=principal.id,
            assigned_at=at,
        )
        db.add(row)
        by_key[kind, ident] = row
        if ident is not None:
            (added_users if kind == "u" else added_teams).append(ident)
    await db.flush()
    if data.primary_user_id is not None:
        primary = by_key["u", data.primary_user_id]
        if not primary.is_primary:
            primary.is_primary = True
            await db.flush()
        await sla.complete_sla_clock(ctx, db, demand.id, "assignment", at=at)
    expanded = await directory.team_member_ids(db, added_teams)
    emit(
        db,
        DemandAssigned(
            tenant_id=demand.tenant_id,
            demand_id=demand.id,
            added_user_ids=tuple(added_users),
            expanded_team_member_ids=tuple(sorted(expanded - set(added_users))),
            primary_user_id=data.primary_user_id,
        ),
    )
    await timeline(
        ctx,
        db,
        demand.id,
        f"Assignment updated ({len(user_ids)} user(s), {len(team_ids)} team(s))",
        primary_user_id=str(data.primary_user_id) if data.primary_user_id else None,
    )
    return await _out(db, await _active(db, demand.id))


async def _row_for(db: AsyncSession, demand_id: uuid.UUID, user_id: uuid.UUID) -> DemandAssignment | None:
    rows = await _active(db, demand_id)
    direct = next((r for r in rows if r.user_id == user_id), None)
    if direct is not None:
        return direct
    teams = await directory.user_team_ids(db, user_id)
    return next((r for r in rows if r.team_id is not None and r.team_id in teams), None)


async def _stamp(
    db: AsyncSession, demand_id: uuid.UUID, user_id: uuid.UUID, attr: str, at: datetime | None
) -> bool:
    row = await _row_for(db, demand_id, user_id)
    if row is None or getattr(row, attr) is not None:
        return False
    with acting_as(db, "system"):
        setattr(row, attr, at or now())
        await db.flush()
    return True


async def record_first_activity(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    demand_id: uuid.UUID,
    user_id: uuid.UUID,
    at: datetime | None = None,
) -> bool:
    return await _stamp(db, demand_id, user_id, "first_activity_at", at)


async def record_first_application(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    demand_id: uuid.UUID,
    user_id: uuid.UUID,
    at: datetime | None = None,
) -> bool:
    return await _stamp(db, demand_id, user_id, "first_application_at", at)


async def record_first_submission(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    user_id: uuid.UUID,
    at: datetime | None = None,
) -> bool:
    stamped = await _stamp(db, demand_id, user_id, "first_submission_at", at)
    with acting_as(db, "system"):
        await sla.complete_sla_clock(ctx, db, demand_id, "first_submission", at=at)
    return stamped
