# ruff: file-ignore[line-too-long]
"""Projects (Epic 45, FR-228/FR-231): create / edit / list / status transitions under a SOW.

AD-4: ``projects.status`` changes only through ``transition`` against ``TRANSITIONS``; anything else is 409
``transition_not_allowed``. ``draft -> active`` needs an Active SOW (``contract_type='sow'``) of the project's
client (409 ``contract_not_active``; 422 ``sow_required`` without one). Visibility: ``project:read|write`` data
scope on ``owner_id`` / ``business_unit_id`` (404 outside it).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.sequences import next_human_id
from app.models.clients import Client
from app.models.commercials import Contract
from app.models.projects import Project, ProjectBurn
from app.schemas.projects import ProjectCreate, ProjectListItem, ProjectOut, ProjectUpdate
from app.services.projects.common import (
    READ,
    WRITE,
    conflict,
    get_project,
    invalid,
    now,
    scoped,
    tenant_of,
    timeline,
    user_id,
)

SEQUENCE_KEY = "project"
HUMAN_ID_PREFIX = "PRJ"
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"active", "cancelled"}),
    "active": frozenset({"on_hold", "completed", "cancelled"}),
    "on_hold": frozenset({"active", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}
EDITABLE_STATUSES = frozenset({"draft", "active", "on_hold"})


# ================================================================ output
async def outs(db: AsyncSession, rows: Sequence[Project]) -> list[ProjectOut]:
    from app.services import entity_refs

    clients = dict(
        (
            await db.execute(select(Client.id, Client.name).where(Client.id.in_({r.client_id for r in rows})))
        ).all()
    )
    contract_ids = {r.contract_id for r in rows if r.contract_id}
    contracts = (
        dict(
            (
                await db.execute(select(Contract.id, Contract.human_id).where(Contract.id.in_(contract_ids)))
            ).all()
        )
        if contract_ids
        else {}
    )
    owners = await entity_refs.users(db, {r.owner_id for r in rows if r.owner_id})
    out: list[ProjectOut] = []
    for r in rows:
        owner = owners.get(r.owner_id) if r.owner_id else None
        out.append(
            ProjectOut(
                id=r.id,
                human_id=r.human_id,
                name=r.name,
                client_id=r.client_id,
                client_name=clients.get(r.client_id),
                contract_id=r.contract_id,
                contract_human_id=contracts.get(r.contract_id) if r.contract_id else None,
                owner_id=r.owner_id,
                owner_name=owner.name if owner else None,
                business_unit_id=r.business_unit_id,
                legal_entity_id=r.legal_entity_id,
                currency=r.currency,
                status=r.status,  # type: ignore[arg-type]
                start_date=r.start_date,
                end_date=r.end_date,
                description=r.description,
                risk_note=r.risk_note,
                archived_at=r.archived_at,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
        )
    return out


async def out(db: AsyncSession, row: Project) -> ProjectOut:
    return (await outs(db, [row]))[0]


# ================================================================ validation
async def _sow(db: AsyncSession, contract_id: uuid.UUID, client_id: uuid.UUID) -> Contract:
    contract = (await db.execute(select(Contract).where(Contract.id == contract_id))).scalar_one_or_none()
    if contract is None or contract.contract_type != "sow" or contract.client_id != client_id:
        raise invalid("contract_invalid", "Choose a SOW of the project's client", field="contract_id")
    return contract


def _check_dates(start: Any, end: Any) -> None:
    if start is not None and end is not None and end < start:
        raise invalid(
            "project_dates_invalid", "The end date must be on or after the start date", field="end_date"
        )


# ================================================================ writes
async def create_project(ctx: RequestContext, db: AsyncSession, data: ProjectCreate) -> ProjectOut:
    client = (await db.execute(select(Client.id).where(Client.id == data.client_id))).first()
    if client is None:
        raise invalid("client_invalid", "Unknown client", field="client_id")
    _check_dates(data.start_date, data.end_date)
    row = Project(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, SEQUENCE_KEY, HUMAN_ID_PREFIX),
        name=data.name,
        client_id=data.client_id,
        owner_id=data.owner_id or user_id(ctx),
        business_unit_id=data.business_unit_id or getattr(ctx.principal, "business_unit_id", None),
        start_date=data.start_date,
        end_date=data.end_date,
        description=data.description,
        risk_note=data.risk_note,
        status="draft",
    )
    if data.contract_id is not None:
        sow = await _sow(db, data.contract_id, data.client_id)
        row.contract_id, row.legal_entity_id, row.currency = sow.id, sow.legal_entity_id, sow.currency
        row.start_date = row.start_date or sow.start_date
        row.end_date = row.end_date or sow.end_date
    db.add(row)
    await db.flush()
    await timeline(ctx, db, row.id, f"{row.human_id}: project created", {"contract_id": str(row.contract_id)})
    await db.refresh(row)
    return await out(db, row)


async def update_project(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID, data: ProjectUpdate
) -> ProjectOut:
    row = await get_project(ctx, db, project_id, permission=WRITE, lock=True)
    if row.status not in EDITABLE_STATUSES:
        raise conflict(
            "project_not_editable", "Completed or cancelled projects cannot be edited", status=row.status
        )
    changes = data.model_dump(exclude_unset=True)
    if "contract_id" in changes and changes["contract_id"] != row.contract_id:
        if row.status != "draft":
            raise conflict("project_not_editable", "The SOW changes only while the project is a draft")
        if changes["contract_id"] is None:
            row.legal_entity_id = row.currency = None
        else:
            sow = await _sow(db, changes["contract_id"], row.client_id)
            row.legal_entity_id, row.currency = sow.legal_entity_id, sow.currency
    if changes.get("name", "") is None:
        raise invalid("name_required", "The project needs a name", field="name")
    _check_dates(changes.get("start_date", row.start_date), changes.get("end_date", row.end_date))
    for key, value in changes.items():
        setattr(row, key, value)
    await db.flush()
    await timeline(ctx, db, row.id, f"{row.human_id}: details updated", {"fields": sorted(changes)})
    await db.refresh(row)
    return await out(db, row)


async def transition(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID, to: str) -> ProjectOut:
    row = await get_project(ctx, db, project_id, permission=WRITE, lock=True)
    if row.status == to:
        return await out(db, row)
    if to not in TRANSITIONS.get(row.status, frozenset()):
        raise conflict(
            "transition_not_allowed",
            f"A project cannot move from {row.status} to {to}",
            from_status=row.status,
            to_status=to,
        )
    if to == "active":
        if row.contract_id is None:
            raise invalid(
                "sow_required", "Link the project to its SOW before activating it", field="contract_id"
            )
        sow = await _sow(db, row.contract_id, row.client_id)
        if sow.status != "active":
            raise conflict("contract_not_active", "The SOW is not active", status=sow.status)
    from_status = row.status
    row.status = to
    if to in ("completed", "cancelled"):
        row.archived_at = row.archived_at or now()
    await db.flush()
    await timeline(ctx, db, row.id, f"{row.human_id}: {from_status} -> {to}", {"from": from_status, "to": to})
    await db.refresh(row)
    return await out(db, row)


# ================================================================ reads
async def get_project_out(ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID) -> ProjectOut:
    return await out(db, await get_project(ctx, db, project_id))


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_projects(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: str | None = None,
    client_id: uuid.UUID | None = None,
    contract_id: uuid.UUID | None = None,
    q: str | None = None,
    include_archived: bool = False,
) -> list[ProjectListItem]:
    from app.services.projects import overview

    stmt = scoped(ctx, select(Project), READ)
    if status:
        stmt = stmt.where(Project.status == status)
    elif not include_archived:
        stmt = stmt.where(Project.archived_at.is_(None))
    if client_id is not None:
        stmt = stmt.where(Project.client_id == client_id)
    if contract_id is not None:
        stmt = stmt.where(Project.contract_id == contract_id)
    if q:
        stmt = stmt.where(or_(Project.name.ilike(_like(q)), Project.human_id.ilike(_like(q))))
    rows = list((await db.execute(stmt.order_by(Project.created_at.desc()).limit(500))).scalars())
    if not rows:
        return []
    ids = [r.id for r in rows]
    burn = dict(
        (
            await db.execute(
                select(ProjectBurn.project_id, func.max(ProjectBurn.pct))
                .where(ProjectBurn.project_id.in_(ids))
                .group_by(ProjectBurn.project_id)
            )
        ).all()
    )
    risks = await overview.risk_counts(db, rows)
    return [
        ProjectListItem(**o.model_dump(), burn_pct=burn.get(o.id), risks_count=risks.get(o.id, 0))
        for o in await outs(db, rows)
    ]


async def quick_hits(ctx: RequestContext, db: AsyncSession, q: str, limit: int) -> list[Project]:
    term = (q or "").strip()
    if not term:
        return []
    stmt = scoped(
        ctx,
        select(Project).where(or_(Project.name.ilike(_like(term)), Project.human_id.ilike(_like(term)))),
        READ,
    )
    return list(
        (await db.execute(stmt.order_by(Project.updated_at.desc()).limit(max(1, min(limit, 50))))).scalars()
    )
