"""Demand records (FR-10, story 4.1): create / edit / list / archive, AI-field confirmation, response
building (progress, SLA, compensation masking) and the pipeline hooks later epics call.

Visibility: ``scope_filter(demand:read, owner_id, business_unit_id)`` OR the caller is an assignee (directly
or through a team). ``req_id``, ``status``, counters and progress are read-only.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import can_view_compensation, check, is_compensation_field, mask_compensation
from app.core.sequences import next_human_id
from app.events.demands import DemandCreated
from app.models.demands import Demand, DemandAssignment, DemandSkill, DemandSlaClock
from app.schemas.demands import (
    DemandCreate,
    DemandFilters,
    DemandOut,
    DemandSkillIn,
    DemandSkillOut,
    DemandUpdate,
    PersonRef,
    SlaClockOut,
)
from app.services import directory_service as directory
from app.services.demands import lifecycle, progress, sla
from app.services.demands.common import (
    CREATE,
    DELETE,
    READ,
    UPDATE,
    access_condition,
    acting_as,
    assigned_condition,
    demand_settings,
    get_for,
    now,
    principal_of,
    timeline,
)
from app.services.demands.constants import REQ_SEQUENCE_KEY

TERMINAL = frozenset({"cancelled", "closed"})
# Scalar columns a user may write through create/update (skills and interview plan handled separately).
_SCALAR_FIELDS: tuple[str, ...] = (
    "title",
    "client_id",
    "end_client",
    "hiring_manager_contact_id",
    "positions",
    "department",
    "employment_type",
    "country",
    "location",
    "work_mode",
    "min_experience_years",
    "max_experience_years",
    "education",
    "certifications",
    "work_authorization",
    "clearance",
    "languages",
    "shift",
    "travel_percent",
    "start_date",
    "target_closure_date",
    "priority",
    "bill_rate",
    "pay_rate_min",
    "pay_rate_max",
    "currency",
    "rate_unit",
    "submission_limit",
    "visibility",  # Epic 36
)
_SKILL_FIELDS = ("skills", "mandatory_skills", "preferred_skills")
PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _field_error(field: str, message: str, code: str = "validation_failed") -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"fields": [{"field": field, "message": message}]})


# ================================================================ clients (Epic 2)
async def _check_client(db: AsyncSession, client_id: uuid.UUID) -> None:
    from app.services import clients_service

    try:
        await clients_service.assert_client_accepts_demands(db, client_id)
    except NotFound as exc:
        raise _field_error("client_id", "Unknown client", "invalid_reference") from exc


async def _check_contact(db: AsyncSession, contact_id: uuid.UUID | None, client_id: uuid.UUID) -> None:
    if contact_id is None:
        return
    from app.services import clients_service

    if not await clients_service.contact_belongs_to_client(db, contact_id, client_id):
        raise _field_error(
            "hiring_manager_contact_id",
            "The contact does not belong to the demand's client",
            "invalid_contact",
        )


# ================================================================ skills
def _skill_inputs(data: DemandCreate | DemandUpdate) -> list[DemandSkillIn] | None:
    sent = data.model_fields_set
    if not any(f in sent for f in _SKILL_FIELDS):
        return None
    skills = list(data.skills or [])
    skills += [DemandSkillIn(label=s, kind="mandatory") for s in data.mandatory_skills or []]
    skills += [DemandSkillIn(label=s, kind="preferred") for s in data.preferred_skills or []]
    return skills


async def _resolve(ctx: RequestContext, db: AsyncSession, item: DemandSkillIn) -> tuple[Any, str]:
    """Return (SkillRef | None, label). Falls back to label-only if the candidates service is absent."""
    try:
        from app.schemas.candidates import SkillRefIn
        from app.services.candidates import skills as skill_catalog
    except ImportError:  # pragma: no cover - Epic 3 absent
        return None, item.label
    if item.skill_id is not None and item.skill_ref_kind is not None:
        ref = await skill_catalog.get_ref(db, SkillRefIn(kind=item.skill_ref_kind, id=item.skill_id))
        if ref is not None:
            return ref, ref.name
    ref = await skill_catalog.resolve_skill(ctx, db, item.label, created_from="demand")
    return ref, (ref.name if ref is not None else item.label)


async def set_skills(
    ctx: RequestContext, db: AsyncSession, demand: Demand, items: list[DemandSkillIn]
) -> None:
    for row in (await db.execute(select(DemandSkill).where(DemandSkill.demand_id == demand.id))).scalars():
        await db.delete(row)
    await db.flush()
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(items):
        ref, label = await _resolve(ctx, db, item)
        key = (item.kind, (f"{ref.kind}:{ref.id}" if ref is not None else label.lower()))
        if key in seen:
            continue
        seen.add(key)
        db.add(
            DemandSkill(
                id=uuid.uuid4(),
                tenant_id=demand.tenant_id,
                demand_id=demand.id,
                skill_id=ref.id if ref is not None else None,
                skill_ref_kind=ref.kind if ref is not None else None,
                skill_label=label[:200],
                kind=item.kind,
                min_years=item.min_years,
                position=i,
                min_proficiency=item.min_proficiency,  # Epic 37
                expand=item.expand,  # Epic 37
            )
        )
    await db.flush()


async def _skills_for(db: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[DemandSkillOut]]:
    out: dict[uuid.UUID, list[DemandSkillOut]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = list(
        (
            await db.execute(
                select(DemandSkill)
                .where(DemandSkill.demand_id.in_(ids))
                .order_by(DemandSkill.demand_id, DemandSkill.position)
            )
        ).scalars()
    )
    statuses: dict[uuid.UUID, str] = {}
    try:
        from app.services.candidates import skills as skill_catalog

        gmap, tmap = await skill_catalog.refs_for(
            db,
            [r.skill_id for r in rows if r.skill_id and r.skill_ref_kind == "global"],
            [r.skill_id for r in rows if r.skill_id and r.skill_ref_kind == "tenant"],
        )
        statuses = {k: v.status for k, v in {**gmap, **tmap}.items()}
    except ImportError:  # pragma: no cover
        pass
    for r in rows:
        out[r.demand_id].append(
            DemandSkillOut(
                label=r.skill_label,
                skill_id=r.skill_id,
                skill_ref_kind=r.skill_ref_kind,
                skill_status=statuses.get(r.skill_id) if r.skill_id else None,
                kind=r.kind,
                min_years=r.min_years,
                min_proficiency=r.min_proficiency,  # Epic 37
                expand=r.expand,  # Epic 37
            )
        )
    return out


# ================================================================ output
async def build_out(ctx: RequestContext, db: AsyncSession, demands: Sequence[Demand]) -> list[dict[str, Any]]:
    from app.models.clients import Client

    principal = principal_of(ctx)
    ids = [d.id for d in demands]
    skills = await _skills_for(db, ids)
    clocks = await sla.clocks_for(db, ids)
    client_names: dict[uuid.UUID, str] = {}
    client_ids = {d.client_id for d in demands}
    if client_ids:
        client_names = {
            k: str(v)
            for k, v in (
                await db.execute(select(Client.id, Client.name).where(Client.id.in_(client_ids)))
            ).all()
        }
    primaries: dict[uuid.UUID, uuid.UUID | None] = {}
    if ids:
        primaries = dict(
            (
                await db.execute(
                    select(DemandAssignment.demand_id, DemandAssignment.user_id).where(
                        DemandAssignment.demand_id.in_(ids),
                        DemandAssignment.is_primary.is_(True),
                        DemandAssignment.removed_at.is_(None),
                    )
                )
            )
            .tuples()
            .all()
        )
    names = await directory.user_names(db, [u for u in primaries.values() if u])
    show_comp = can_view_compensation(principal)
    out: list[dict[str, Any]] = []
    for d in demands:
        demand_clocks = [
            SlaClockOut(
                milestone=c.milestone,
                ref_id=c.ref_id,
                status=c.status,
                started_at=c.started_at,
                due_at=c.due_at,
                completed_at=c.completed_at,
            )
            for c in clocks[d.id]
        ]
        running = [c.status for c in demand_clocks if c.completed_at is None] if d.status == "open" else []
        primary = primaries.get(d.id)
        item = DemandOut(
            **{k: getattr(d, k) for k in DemandOut.model_fields if hasattr(Demand, k) and k != "progress"},
            client_name=client_names.get(d.client_id),
            margin_percent=lifecycle.margin_percent(d),
            progress=progress.compute(
                positions=d.positions,
                positions_filled=d.positions_filled,
                applications=d.applications_count,
                submissions=d.submissions_count,
                interviews_scheduled=d.interviews_scheduled_count,
                offers_released=d.offers_released_count,
            ),
            skills=skills[d.id],
            primary_recruiter=PersonRef(id=primary, name=names.get(primary)) if primary else None,
            sla=demand_clocks,
            sla_status=sla.worst(running),
        )
        data: dict[str, Any] = item.model_dump(mode="json")
        if not show_comp:
            data = mask_compensation(data, principal)
            data["masked_fields"] = sorted(k for k in DemandOut.model_fields if is_compensation_field(k))
        out.append(data)
    return out


async def demand_out(ctx: RequestContext, db: AsyncSession, demand: Demand) -> dict[str, Any]:
    await db.refresh(demand)
    return (await build_out(ctx, db, [demand]))[0]


# ================================================================ create / update
def _validate_ranges(demand: Demand) -> None:
    if (
        demand.min_experience_years is not None
        and demand.max_experience_years is not None
        and Decimal(demand.min_experience_years) > Decimal(demand.max_experience_years)
    ):
        raise _field_error("min_experience_years", "min_experience_years must be <= max_experience_years")
    if (
        demand.pay_rate_min is not None
        and demand.pay_rate_max is not None
        and Decimal(demand.pay_rate_min) > Decimal(demand.pay_rate_max)
    ):
        raise _field_error("pay_rate_min", "pay_rate_min must be <= pay_rate_max")
    if demand.positions < (demand.positions_filled or 0):
        raise _field_error("positions", "positions cannot be below the positions already filled")


async def create_demand_row(
    ctx: RequestContext,
    db: AsyncSession,
    data: DemandCreate,
    *,
    actor_kind: str = "user",
    ai_extracted_fields: Sequence[str] = (),
    intake_ai_action_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
) -> Demand:
    """``owner_id`` (Epic 15 Won conversion): the Account Manager owns the draft Demand instead of the caller."""
    principal = principal_of(ctx)
    check(principal, CREATE)
    await _check_client(db, data.client_id)
    await _check_contact(db, data.hiring_manager_contact_id, data.client_id)
    settings = await demand_settings(ctx, db)
    req_id = await next_human_id(db, REQ_SEQUENCE_KEY, prefix=settings.req_prefix, width=4)
    values = {
        k: getattr(data, k)
        for k in _SCALAR_FIELDS
        if k in data.model_fields_set or k in ("title", "client_id")
    }
    values.pop("positions", None)
    if data.priority is None:
        values.pop("priority", None)
    if data.visibility is None:
        values.pop("visibility", None)
    for list_field in ("certifications", "work_authorization", "languages"):
        if values.get(list_field) is None:
            values.pop(list_field, None)
    demand = Demand(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        req_id=req_id,
        status="draft",
        owner_id=owner_id or principal.id,
        business_unit_id=principal.business_unit_id,
        positions=data.positions,
        interview_plan=[s.model_dump() for s in data.interview_plan or []],
        ai_extracted_fields=list(ai_extracted_fields),
        intake_ai_action_id=intake_ai_action_id,
        **values,
    )
    _validate_ranges(demand)
    with acting_as(db, actor_kind, None if actor_kind == "ai" else principal.id):
        db.add(demand)
        await db.flush()
    skills = _skill_inputs(data)
    if skills:
        await set_skills(ctx, db, demand, skills)
    emit(db, DemandCreated(tenant_id=demand.tenant_id, demand_id=demand.id, actor_kind=actor_kind))
    await timeline(ctx, db, demand.id, f"Demand {req_id} created", actor_kind=actor_kind)
    return demand


async def create_demand(ctx: RequestContext, db: AsyncSession, data: DemandCreate) -> dict[str, Any]:
    demand = await create_demand_row(ctx, db, data)
    return await demand_out(ctx, db, demand)


async def create_draft(
    ctx: RequestContext,
    db: AsyncSession,
    data: DemandCreate,
    *,
    workforce_plan_line_id: uuid.UUID | None = None,
    project_role_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Epic 36 contract (AD-39): create a draft Demand linked to a workforce plan line (Epic 44) and / or a
    project role (Epic 45) -- the only writer of those link columns. DemandOut-shaped, as ``create_demand``.

    422 ``unknown_plan_line`` / ``unknown_project_role``; 422 ``project_role_client_mismatch`` (the role's project
    belongs to another client); 422 ``plan_line_client_mismatch`` (the line names another client).
    """
    from app.models.projects import Project, ProjectRole
    from app.models.workforce import WorkforcePlanLine

    if workforce_plan_line_id is not None:
        line = await db.get(WorkforcePlanLine, workforce_plan_line_id)
        if line is None:
            raise _field_error("workforce_plan_line_id", "Unknown workforce plan line", "unknown_plan_line")
        if line.client_id is not None and line.client_id != data.client_id:
            raise _field_error(
                "workforce_plan_line_id", "The plan line belongs to another client", "plan_line_client_mismatch"
            )
    if project_role_id is not None:
        role = await db.get(ProjectRole, project_role_id)
        if role is None:
            raise _field_error("project_role_id", "Unknown project role", "unknown_project_role")
        project = await db.get(Project, role.project_id)
        if project is None or project.client_id != data.client_id:
            raise _field_error(
                "project_role_id", "The project role belongs to another client", "project_role_client_mismatch"
            )
    demand = await create_demand_row(ctx, db, data)
    if workforce_plan_line_id is not None or project_role_id is not None:
        demand.workforce_plan_line_id = workforce_plan_line_id
        demand.project_role_id = project_role_id
        await db.flush()
    return await demand_out(ctx, db, demand)


async def update_demand(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: DemandUpdate
) -> dict[str, Any]:
    demand = await get_for(ctx, db, demand_id, UPDATE)
    if demand.status in TERMINAL or demand.archived_at is not None:
        raise ConflictError(
            "A cancelled, closed or archived demand cannot be edited", code="demand_not_editable"
        )
    sent = data.model_fields_set
    for field in ("title", "client_id", "positions", "priority", "visibility"):
        if field in sent and getattr(data, field) is None:
            raise _field_error(field, f"{field} cannot be empty")
    if "client_id" in sent and data.client_id != demand.client_id:
        if demand.status != "draft":
            raise ConflictError(
                "The client can only change while the demand is a draft", code="client_locked"
            )
        await _check_client(db, data.client_id)  # type: ignore[arg-type]
    edited: set[str] = set()
    for field in _SCALAR_FIELDS:
        if field in sent:
            value = getattr(data, field)
            if field in ("certifications", "work_authorization", "languages") and value is None:
                value = []
            setattr(demand, field, value)
            edited.add(field)
    if "interview_plan" in sent:
        demand.interview_plan = [s.model_dump() for s in data.interview_plan or []]
    contact_changed = "hiring_manager_contact_id" in sent or "client_id" in sent
    if contact_changed:
        await _check_contact(db, demand.hiring_manager_contact_id, demand.client_id)
    _validate_ranges(demand)
    skills = _skill_inputs(data)
    if skills is not None:
        await set_skills(ctx, db, demand, skills)
        edited |= {"mandatory_skills", "preferred_skills", "skills"}
    cleared = [f for f in demand.ai_extracted_fields if f in edited]
    if cleared:
        demand.ai_extracted_fields = [f for f in demand.ai_extracted_fields if f not in edited]
        await _record_ai_decision(ctx, db, demand, "edited")
    await db.flush()
    return await demand_out(ctx, db, demand)


async def _record_ai_decision(ctx: RequestContext, db: AsyncSession, demand: Demand, decision: str) -> None:
    if demand.intake_ai_action_id is None:
        return
    from app.services import ai

    try:
        await ai.record_decision(ctx, db, demand.intake_ai_action_id, decision)
    except NotFound:  # the action row is gone (e.g. erased); nothing to record
        return


async def confirm_ai_fields(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, fields: list[str] | None
) -> dict[str, Any]:
    demand = await get_for(ctx, db, demand_id, UPDATE)
    current = list(demand.ai_extracted_fields)
    targets = set(current if fields is None else fields)
    unknown = sorted(targets - set(current))
    if unknown:
        raise ValidationFailed(
            "These fields are not awaiting confirmation",
            code="unknown_ai_fields",
            details={"fields": unknown},
        )
    demand.ai_extracted_fields = [f for f in current if f not in targets]
    await db.flush()
    if not demand.ai_extracted_fields and demand.intake_ai_action_id is not None:
        from app.services import ai

        actions = await ai.list_actions(ctx, db, "demand", demand.id)
        previous = next((a.decision for a in actions if a.id == demand.intake_ai_action_id), None)
        if previous != "edited":
            await _record_ai_decision(ctx, db, demand, "accepted")
    return await demand_out(ctx, db, demand)


async def get_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> dict[str, Any]:
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    return (await build_out(ctx, db, [demand]))[0]


async def archive_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> dict[str, Any]:
    demand = await get_for(ctx, db, demand_id, DELETE)
    if demand.status not in ("draft", "cancelled", "closed"):
        raise ConflictError("Only draft, cancelled or closed demands can be archived", code="demand_active")
    if demand.archived_at is None:
        demand.archived_at = now()
        await db.flush()
        await timeline(ctx, db, demand.id, "Demand archived")
    return await demand_out(ctx, db, demand)


# ================================================================ list
def _visible_stmt(ctx: RequestContext) -> Select[tuple[Demand]]:
    principal = principal_of(ctx)
    stmt = select(Demand)
    cond = access_condition(principal, READ)
    return stmt if cond is None else stmt.where(cond)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_query(ctx: RequestContext, db: AsyncSession, filters: DemandFilters) -> Select[tuple[Demand]]:
    """Return the scoped, filtered and ordered demand list query (Epic 11: the single source of
    ``GET /demands`` and the demands export)."""
    principal = principal_of(ctx)
    stmt = _visible_stmt(ctx)
    if not filters.include_archived:
        stmt = stmt.where(Demand.archived_at.is_(None))
    if filters.status:
        stmt = stmt.where(Demand.status.in_(filters.status))
    if filters.client_id:
        stmt = stmt.where(Demand.client_id == filters.client_id)
    if filters.priority:
        stmt = stmt.where(Demand.priority.in_(filters.priority))
    if filters.owner_id:
        stmt = stmt.where(Demand.owner_id == filters.owner_id)
    assigned_to = filters.assigned_to or filters.assignee
    if assigned_to == "me":
        stmt = stmt.where(assigned_condition(principal))
    elif assigned_to:
        user_id = uuid.UUID(assigned_to)
        teams = await directory.user_team_ids(db, user_id)
        who = [DemandAssignment.user_id == user_id]
        if teams:
            who.append(DemandAssignment.team_id.in_(teams))
        stmt = stmt.where(
            Demand.id.in_(
                select(DemandAssignment.demand_id).where(DemandAssignment.removed_at.is_(None), or_(*who))
            )
        )
    if filters.q:
        like = _like(filters.q)
        stmt = stmt.where(or_(Demand.title.ilike(like), Demand.req_id.ilike(like)))
    sla_status = filters.sla_status or filters.sla
    if sla_status:
        stmt = stmt.where(
            Demand.status == "open",
            Demand.id.in_(
                select(DemandSlaClock.demand_id).where(
                    DemandSlaClock.completed_at.is_(None), DemandSlaClock.status == sla_status
                )
            ),
        )
    if filters.business_unit_ids:
        stmt = stmt.where(Demand.business_unit_id.in_(filters.business_unit_ids))
    if filters.client_owner == "me":
        from app.models.clients import Client

        stmt = stmt.where(Demand.client_id.in_(select(Client.id).where(Client.owner_id == principal.id)))
    if filters.opened_from or filters.opened_to:
        tz = ZoneInfo(await directory.tenant_timezone(db, principal.tenant_id))  # type: ignore[arg-type]
        if filters.opened_from:
            start = datetime.combine(filters.opened_from, time.min, tzinfo=tz)
            stmt = stmt.where(Demand.opened_at >= start)
        if filters.opened_to:
            end = datetime.combine(filters.opened_to + timedelta(days=1), time.min, tzinfo=tz)
            stmt = stmt.where(Demand.opened_at < end)
    sort = filters.sort
    desc = sort.startswith("-")
    key = sort.lstrip("-")
    if key == "priority":
        column: Any = case(PRIORITY_RANK, value=Demand.priority)
    elif key == "target_closure_date":
        column = Demand.target_closure_date
    else:
        column = Demand.created_at
    order = column.desc().nulls_last() if desc else column.asc().nulls_last()
    return stmt.order_by(order, Demand.req_id.desc(), Demand.id)


async def list_demands(
    ctx: RequestContext, db: AsyncSession, filters: DemandFilters, params: PageParams
) -> Page[dict[str, Any]]:
    stmt = await list_query(ctx, db, filters)
    page = await paginate(db, stmt, params)
    items = await build_out(ctx, db, list(page.items))
    return Page[dict[str, Any]](items=items, total=page.total, page=page.page, page_size=page.page_size)


async def list_for_client(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Client 360 section: the caller's visible, non-archived demands of one client (compact rows)."""
    principal = principal_of(ctx)
    if READ not in principal.permissions:
        return []
    stmt = (
        _visible_stmt(ctx)
        .where(Demand.client_id == client_id, Demand.archived_at.is_(None))
        .order_by(Demand.created_at.desc(), Demand.id)
        .limit(200)
    )
    rows = list((await db.execute(stmt)).scalars())
    return [
        {
            "id": str(d.id),
            "req_id": d.req_id,
            "title": d.title,
            "status": d.status,
            "priority": d.priority,
            "positions": d.positions,
            "positions_filled": d.positions_filled,
            "progress": progress.compute(
                positions=d.positions,
                positions_filled=d.positions_filled,
                applications=d.applications_count,
                submissions=d.submissions_count,
                interviews_scheduled=d.interviews_scheduled_count,
                offers_released=d.offers_released_count,
            ),
            "created_at": d.created_at.isoformat(),
        }
        for d in rows
    ]


async def count_for_client(ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID) -> tuple[int, int]:
    """(visible demands, visible open demands) of one client."""
    principal = principal_of(ctx)
    if READ not in principal.permissions:
        return 0, 0
    base = _visible_stmt(ctx).where(Demand.client_id == client_id, Demand.archived_at.is_(None)).subquery()
    total, open_ = (
        await db.execute(select(func.count(), func.count().filter(base.c.status == "open")).select_from(base))
    ).one()
    return int(total), int(open_)


# ================================================================ hooks for later epics (actor system)
async def record_pipeline_counts(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    demand_id: uuid.UUID,
    *,
    applications: int | None = None,
    submissions: int | None = None,
    interviews_scheduled: int | None = None,
    offers_released: int | None = None,
) -> None:
    demand = await db.get(Demand, demand_id, with_for_update=True)
    if demand is None:
        raise NotFound("Demand not found")
    with acting_as(db, "system"):
        for attr, value in (
            ("applications_count", applications),
            ("submissions_count", submissions),
            ("interviews_scheduled_count", interviews_scheduled),
            ("offers_released_count", offers_released),
        ):
            if value is not None:
                setattr(demand, attr, max(int(value), 0))
        await db.flush()


async def apply_positions_filled(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, filled: int
) -> None:
    """Store the accepted-offer count the count; ``filled == positions`` on an Open demand -> Filled."""
    demand = await db.get(Demand, demand_id, with_for_update=True)
    if demand is None:
        raise NotFound("Demand not found")
    with acting_as(db, "system"):
        demand.positions_filled = max(int(filled), 0)
        await db.flush()
        if demand.status == "open" and demand.positions_filled >= demand.positions:
            await lifecycle.transition_demand(ctx, db, demand, "filled", "system")
        await db.flush()
