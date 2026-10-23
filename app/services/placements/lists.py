"""Placement reads (Epic 19 A9, FR-109): list with filters (+ registered filters), detail, history, touchpoints and
the pre-joining tracker. Own scope = recruiter or Account Manager of the placement."""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.fiscal import today_for
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal, scope_condition
from app.models.placements import Placement, PlacementStatusHistory, PlacementTouchpoint, StartGate
from app.schemas.placements_api import (
    PlacementListItem,
    PlacementOut,
    PreJoiningRow,
    StatusHistoryOut,
    TouchpointOut,
)
from app.services.placements.common import PRE_START, get_placement, names, not_found, settings_row
from app.services.placements.registry import placement_filters

SAVED_VIEWS: dict[str, dict[str, Any]] = {
    "ending_30": {"label": "Ending within 30 days", "ending_within_days": 30},
    "ending_60": {"label": "Ending within 60 days", "ending_within_days": 60},
    "non_compliant": {"label": "Non-compliant", "compliance": "non_compliant"},
}


def _ending_soon(p: Placement, days: int = 30, today: date | None = None) -> bool:
    end = p.planned_end_date or p.end_date
    if end is None or p.status not in ("active", "offboarding", *PRE_START):
        return False
    t = today or date.today()
    return t <= end <= t + timedelta(days=days)


def list_item(
    p: Placement, candidate_name: str | None, client_name: str | None, ending_days: int = 30
) -> PlacementListItem:
    item = PlacementListItem.model_validate(p)
    return item.model_copy(
        update={
            "candidate_name": candidate_name,
            "client_name": client_name,
            "ending_soon": _ending_soon(p, ending_days),
        }
    )


def scoped(stmt: Select[Any], principal: Principal | None) -> Select[Any]:
    if principal is None or getattr(principal, "type", None) != "user":
        return stmt
    a = scope_condition(principal, "placement:read", Placement.recruiter_user_id)
    if a is None:
        return stmt
    b = scope_condition(principal, "placement:read", Placement.account_manager_user_id)
    return stmt.where(or_(a, b) if b is not None else a)


async def visible(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> Placement:
    stmt = scoped(select(Placement).where(Placement.id == placement_id), ctx.principal)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


async def detail(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> PlacementOut:
    from app.models.legal_entities import LegalEntity
    from app.models.placements import WorkerProfile

    p = await visible(ctx, db, placement_id)
    cands, clients = await names(db, candidate_ids={p.candidate_id}, client_ids={p.client_id})
    le_name = (
        await db.execute(select(LegalEntity.name).where(LegalEntity.id == p.legal_entity_id))
    ).scalar_one_or_none()
    wrk = (
        await db.execute(
            select(WorkerProfile.worker_human_id).where(WorkerProfile.candidate_id == p.candidate_id)
        )
    ).scalar_one_or_none()
    settings = await settings_row(db, p.tenant_id)
    out = PlacementOut.model_validate(p)
    return out.model_copy(
        update={
            "candidate_name": cands.get(p.candidate_id),
            "client_name": clients.get(p.client_id),
            "legal_entity_name": le_name,
            "worker_human_id": wrk,
            "ending_soon": _ending_soon(p, settings.ending_soon_days),
        }
    )


async def list_placements(
    ctx: RequestContext,
    db: AsyncSession,
    params: PageParams,
    *,
    client_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    statuses: list[str] | None = None,
    worker_type: str | None = None,
    vendor_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    ending_within_days: int | None = None,
    compliance: str | None = None,
    q: str | None = None,
    view: str | None = None,
    extra: dict[str, str] | None = None,
) -> Page[PlacementListItem]:
    from app.models.candidates import Candidate

    if view:
        preset = SAVED_VIEWS.get(view)
        if preset is None:
            raise ValidationFailed("Unknown saved view", code="unknown_view")
        ending_within_days = ending_within_days or preset.get("ending_within_days")
        compliance = compliance or preset.get("compliance")
    stmt = scoped(select(Placement), ctx.principal)
    if client_id:
        stmt = stmt.where(Placement.client_id == client_id)
    if legal_entity_id:
        stmt = stmt.where(Placement.legal_entity_id == legal_entity_id)
    if statuses:
        stmt = stmt.where(Placement.status.in_(statuses))
    if worker_type:
        stmt = stmt.where(Placement.worker_type == worker_type)
    if vendor_id:
        stmt = stmt.where(Placement.vendor_id == vendor_id)
    if candidate_id:
        stmt = stmt.where(Placement.candidate_id == candidate_id)
    if ending_within_days:
        today = today_for("Asia/Kolkata")
        end = func.coalesce(Placement.end_date, Placement.planned_end_date)
        stmt = stmt.where(
            Placement.status.in_(("active", "offboarding", *PRE_START)),
            end >= today,
            end <= today + timedelta(days=int(ending_within_days)),
        )
    if compliance == "non_compliant":
        stmt = stmt.where(or_(Placement.non_compliant.is_(True), Placement.compliance_blocked.is_(True)))
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Placement.human_id.ilike(term),
                Placement.candidate_id.in_(select(Candidate.id).where(Candidate.full_name.ilike(term))),
            )
        )
    registered = placement_filters()
    for key, value in (extra or {}).items():
        fn = registered.get(key)
        if fn is None:
            raise ValidationFailed(f"Unknown filter {key}", code="unknown_filter", details={"filter": key})
        stmt = fn(stmt, value)
    page = await paginate(db, stmt.order_by(Placement.planned_start_date.desc(), Placement.id), params)
    rows: list[Placement] = list(page.items)
    cands, clients = await names(
        db, candidate_ids={p.candidate_id for p in rows}, client_ids={p.client_id for p in rows}
    )
    settings = await settings_row(db, ctx.tenant_id) if ctx.tenant_id else None
    days = settings.ending_soon_days if settings else 30
    return Page[PlacementListItem](
        items=[list_item(p, cands.get(p.candidate_id), clients.get(p.client_id), days) for p in rows],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def history(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list[StatusHistoryOut]:
    await visible(ctx, db, placement_id)
    rows = (
        await db.execute(
            select(PlacementStatusHistory)
            .where(PlacementStatusHistory.placement_id == placement_id)
            .order_by(PlacementStatusHistory.at)
        )
    ).scalars()
    return [StatusHistoryOut.model_validate(r) for r in rows]


async def touchpoints(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list[TouchpointOut]:
    await visible(ctx, db, placement_id)
    rows = (
        await db.execute(
            select(PlacementTouchpoint)
            .where(PlacementTouchpoint.placement_id == placement_id)
            .order_by(PlacementTouchpoint.due_on)
        )
    ).scalars()
    return [TouchpointOut.model_validate(r) for r in rows]


async def pre_joining(ctx: RequestContext, db: AsyncSession, starts_within: int = 30) -> list[PreJoiningRow]:
    today = today_for("Asia/Kolkata")
    stmt = scoped(
        select(Placement).where(
            Placement.status.in_(PRE_START),
            Placement.planned_start_date <= today + timedelta(days=starts_within),
        ),
        ctx.principal,
    )
    rows = list(
        (
            await db.execute(stmt.order_by(Placement.planned_start_date, Placement.human_id).limit(500))
        ).scalars()
    )
    if not rows:
        return []
    ids = [p.id for p in rows]
    cands, clients = await names(
        db, candidate_ids={p.candidate_id for p in rows}, client_ids={p.client_id for p in rows}
    )
    gate_counts: dict[uuid.UUID, tuple[int, int]] = {}
    for pid, total, unmet in (
        await db.execute(
            select(
                StartGate.placement_id,
                func.count(),
                func.count().filter(
                    StartGate.required.is_(True), StartGate.status.not_in(("met", "waived", "not_required"))
                ),
            )
            .where(StartGate.placement_id.in_(ids))
            .group_by(StartGate.placement_id)
        )
    ).all():
        gate_counts[pid] = (int(total), int(unmet))
    contact: dict[uuid.UUID, Any] = dict(
        (
            await db.execute(
                select(PlacementTouchpoint.placement_id, func.max(PlacementTouchpoint.responded_at))
                .where(PlacementTouchpoint.placement_id.in_(ids))
                .group_by(PlacementTouchpoint.placement_id)
            )
        ).tuples().all()
    )
    out: list[PreJoiningRow] = []
    for p in rows:
        total, unmet = gate_counts.get(p.id, (0, 0))
        out.append(
            PreJoiningRow(
                placement_id=p.id,
                human_id=p.human_id,
                status=p.status,
                candidate_id=p.candidate_id,
                candidate_name=cands.get(p.candidate_id),
                client_name=clients.get(p.client_id),
                role_title=p.role_title,
                planned_start_date=p.planned_start_date,
                days_to_start=(p.planned_start_date - today).days,
                onboarding_pct=p.onboarding_pct,
                bgv_summary=p.bgv_summary,
                gates_total=total,
                gates_unmet=unmet,
                last_contact_at=contact.get(p.id) or p.joining_confirmed_at,
                joining_confirmed_at=p.joining_confirmed_at,
                dropout_risk=p.dropout_risk,
                dropout_risk_rules=list(p.dropout_risk_rules or []),
            )
        )
    return out


async def get_or_404(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> Placement:
    """Visible placement (scope) for writes; then the caller re-locks it."""
    await visible(ctx, db, placement_id)
    return await get_placement(db, placement_id, lock=True)


EXPORT_COLUMNS = (
    "human_id",
    "status",
    "candidate_name",
    "client_name",
    "role_title",
    "worker_type",
    "engagement_kind",
    "planned_start_date",
    "actual_start_date",
    "planned_end_date",
    "end_date",
    "dropout_risk",
    "non_compliant",
    "setup_items",
)
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    text = "" if value is None else (";".join(value) if isinstance(value, list) else str(value))
    return "'" + text if text.startswith(_FORMULA) else text


async def export_csv(ctx: RequestContext, db: AsyncSession, **filters: Any) -> str:
    """CSV of the filtered list (FR-109 export; never PAN / bank / rates). Capped at 5 000 rows."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    page = 1
    while page <= 25:
        result = await list_placements(ctx, db, PageParams(page=page, page_size=200), **filters)
        for item in result.items:
            data = item.model_dump()
            writer.writerow([_cell(data.get(c)) for c in EXPORT_COLUMNS])
        if page * 200 >= result.total:
            break
        page += 1
    return "\ufeff" + buf.getvalue()
