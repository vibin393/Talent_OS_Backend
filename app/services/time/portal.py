"""Worker ("My Work") and Client Portal projections and actions for Timesheets (AD-26/27, FR-80).

Access is checked first in the caller's org-scoped session (worker: ``candidate_id``; client: ``client_id``
plus
``visible_placement_ids`` for restricted Client Users) -- another org's id is 404. The work itself then
runs in an
internal tenant session on behalf of the portal principal (approval requests, ledgers and configs are
internal-only tables), and Client decisions always go through ``approvals_service.decide`` with the Client
``ActorRef`` (the engine checks the step, ``client_any`` permission and delegation). Responses use
``schemas.time_portal`` only: hours, never rates, amounts or margin.
"""

import hashlib
import uuid
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.client_scope import visible_placement_ids
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.approvals import ApprovalRequest
from app.models.placements import Placement
from app.models.time import Timesheet, TimesheetEntry
from app.schemas.time import COMPUTED_CATEGORIES, DayEntryIn
from app.schemas.time_portal import (
    ClientBulkDecideOut,
    ClientBulkPreviewOut,
    ClientBulkSkip,
    ClientTimesheetDay,
    ClientTimesheetDetail,
    ClientTimesheetFlags,
    ClientTimesheetSummary,
    WorkerAdjustmentEntry,
    WorkerCategoryTotal,
    WorkerDaySplit,
    WorkerEntry,
    WorkerSplitPreview,
    WorkerTimesheetOut,
    WorkerTimesheetSummary,
)
from app.services.time import timesheets as ts_service
from app.services.time.approval import CLIENT_KINDS, decide_as
from app.services.time.common import ZERO, fmt_time, get_timesheet, settings_row
from app.services.time.configs import config_version

if TYPE_CHECKING:
    from datetime import date

TWO = Decimal("0.01")


def _not_found() -> NotFound:
    return NotFound("Timesheet not found")


# ================================================================ Worker
async def _own(db: AsyncSession, candidate_id: uuid.UUID, timesheet_id: uuid.UUID) -> None:
    found = (
        await db.execute(
            select(Timesheet.id).where(Timesheet.id == timesheet_id, Timesheet.candidate_id == candidate_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise _not_found()


async def _placement_meta(
    db: AsyncSession, placement_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str | None, str | None]]:
    from app.models.clients import Client

    if not placement_ids:
        return {}
    rows = await db.execute(
        select(Placement.id, Client.name, Placement.role_title)
        .join(Client, Client.id == Placement.client_id, isouter=True)
        .where(Placement.id.in_(placement_ids))
    )
    return {r[0]: (r[1], r[2]) for r in rows.all()}


async def _worker_summaries(db: AsyncSession, rows: list[Timesheet]) -> list[WorkerTimesheetSummary]:
    base = await ts_service.summaries(db, rows)
    meta = await _placement_meta(db, {r.placement_id for r in rows})
    return [
        WorkerTimesheetSummary(
            id=s.id,
            human_id=s.human_id,
            placement_id=s.placement_id,
            client_name=meta.get(s.placement_id, (None, None))[0],
            role_title=meta.get(s.placement_id, (None, None))[1],
            kind=s.kind,
            period_start=s.period_start,
            period_end=s.period_end,
            status=s.status,
            total_hours=s.total_hours,
            submitted_at=s.submitted_at,
        )
        for s in base
    ]


async def _worker_out(ctx: RequestContext, db: AsyncSession, ts: Timesheet) -> WorkerTimesheetOut:
    summary = (await _worker_summaries(db, [ts]))[0]
    cfg = await config_version(db, ts.placement_id, ts.config_version)
    placement = (await db.execute(select(Placement).where(Placement.id == ts.placement_id))).scalar_one()
    entries = await ts_service._entries(db, ts.id)
    adjustments = await ts_service.adjustment_entries(db, ts.id)
    editable = ts.status in ts_service.EDITABLE and ts.kind == "regular"
    enabled = list(cfg.enabled_categories or [])
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for e in entries:
        totals[e.category] += e.hours
    for a in adjustments:
        totals[a.category] += a.delta_hours
    settings = await settings_row(db, ts.tenant_id)
    lo = max(ts.period_start, placement.actual_start_date or ts.period_start)
    hi = min(d for d in (ts.period_end, placement.end_date) if d is not None)
    del ctx
    return WorkerTimesheetOut(
        **summary.model_dump(),
        entry_mode=cfg.entry_mode,
        hours_per_day=cfg.hours_per_day,
        daily_cap=cfg.daily_cap,
        enabled_categories=enabled,
        editable_categories=[c for c in enabled if c not in COMPUTED_CATEGORIES],
        editable=editable,
        can_recall=ts.status == "submitted"
        and not await ts_service._decided_count(db, ts.approval_request_id),
        can_copy_previous=editable and bool(settings.copy_previous_enabled),
        min_date=lo,
        max_date=hi,
        entries=[
            WorkerEntry(
                id=e.id,
                entry_date=e.entry_date,
                category=e.category,
                hours=e.hours,
                entered_hours=e.entered_hours,
                start_time=fmt_time(e.start_time),
                end_time=fmt_time(e.end_time),
                break_minutes=e.break_minutes,
                prefilled=e.prefilled,
                deletable=not e.prefilled or e.prefill_deletable,
                holiday_id=e.holiday_id,
                leave_type_id=e.leave_type_id,
                note=e.note,
            )
            for e in entries
        ],
        adjustment_entries=[
            WorkerAdjustmentEntry(
                entry_date=a.entry_date, category=a.category, delta_hours=a.delta_hours, note=a.note
            )
            for a in adjustments
        ],
        totals=[WorkerCategoryTotal(category=c, hours=h.quantize(TWO)) for c, h in totals.items()],
        rejection_comment=ts.rejection_comment,
    )


async def my_timesheets(
    ctx: RequestContext, wdb: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[WorkerTimesheetSummary]:
    ids = list(
        (await wdb.execute(select(Timesheet.id).where(Timesheet.candidate_id == candidate_id))).scalars()
    )
    async with tenant_session(tenant_id) as db:
        # make sure the current periods exist for the Worker's active placements
        for p in (
            await db.execute(
                select(Placement).where(Placement.candidate_id == candidate_id, Placement.status == "active")
            )
        ).scalars():
            created = await ts_service.create_period_timesheets(ctx, db, p)
            ids += [t.id for t in created]
        rows = list(
            (
                await db.execute(
                    select(Timesheet)
                    .where(Timesheet.id.in_(ids), Timesheet.candidate_id == candidate_id)
                    .order_by(Timesheet.period_start.desc(), Timesheet.adjustment_seq)
                )
            ).scalars()
        )
        return await _worker_summaries(db, rows)


async def my_timesheet(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
) -> WorkerTimesheetOut:
    await _own(wdb, candidate_id, timesheet_id)
    async with tenant_session(tenant_id) as db:
        return await _worker_out(ctx, db, await get_timesheet(db, timesheet_id))


async def _worker_action(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
    action: str,
    **kwargs: Any,
) -> WorkerTimesheetOut:
    await _own(wdb, candidate_id, timesheet_id)
    actor = ActorRef("candidate", candidate_id)
    async with tenant_session(tenant_id) as db:
        ts = await get_timesheet(db, timesheet_id, lock=True)
        if action == "entries":
            await ts_service.upsert_entries(ctx, db, ts, kwargs["days"])
        elif action == "copy_previous":
            await ts_service.copy_previous(ctx, db, ts)
        elif action == "refresh_prefill":
            await ts_service.refresh_prefill(ctx, db, ts)
        elif action == "submit":
            await ts_service.submit(ctx, db, ts, confirm_zero=kwargs.get("confirm_zero", False), actor=actor)
        elif action == "recall":
            await ts_service.recall(ctx, db, ts, actor)
        await db.refresh(ts)
        return await _worker_out(ctx, db, ts)


async def save_entries(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
    days: list[DayEntryIn],
) -> WorkerTimesheetOut:
    return await _worker_action(ctx, wdb, tenant_id, candidate_id, timesheet_id, "entries", days=days)


async def copy_previous(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
) -> WorkerTimesheetOut:
    return await _worker_action(ctx, wdb, tenant_id, candidate_id, timesheet_id, "copy_previous")


async def refresh_prefill(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
) -> WorkerTimesheetOut:
    return await _worker_action(ctx, wdb, tenant_id, candidate_id, timesheet_id, "refresh_prefill")


async def submit(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
    *,
    confirm_zero: bool,
) -> WorkerTimesheetOut:
    return await _worker_action(
        ctx, wdb, tenant_id, candidate_id, timesheet_id, "submit", confirm_zero=confirm_zero
    )


async def recall(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
) -> WorkerTimesheetOut:
    return await _worker_action(ctx, wdb, tenant_id, candidate_id, timesheet_id, "recall")


async def split_preview(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    timesheet_id: uuid.UUID,
) -> WorkerSplitPreview:
    del ctx
    await _own(wdb, candidate_id, timesheet_id)
    async with tenant_session(tenant_id) as db:
        out = await ts_service.split_preview(db, await get_timesheet(db, timesheet_id))
    return WorkerSplitPreview(
        days=[WorkerDaySplit(**d.model_dump()) for d in out.days],
        totals=[WorkerCategoryTotal(**t.model_dump()) for t in out.totals],
    )


# ================================================================ Client
async def _visible_ids(
    ctx: RequestContext, cdb: AsyncSession, ids: list[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Timesheet ids this Client User may see: org RLS (client) + restricted placement scope."""
    principal = ctx.principal
    client_id = getattr(principal, "org_id", None)
    stmt = select(Timesheet.id, Timesheet.placement_id).where(Timesheet.client_id == client_id)
    if ids is not None:
        stmt = stmt.where(Timesheet.id.in_(ids))
    rows = (await cdb.execute(stmt)).all()
    scope = await visible_placement_ids(ctx, cdb)
    return [r[0] for r in rows if scope is None or r[1] in scope]


def _awaiting(principal: Any, request: ApprovalRequest | None) -> bool:
    if request is None or request.status != "pending" or request.current_approver_kind not in CLIENT_KINDS:
        return False
    if request.current_approver_kind == "client_user":
        return request.current_approver_client_user_id == getattr(principal, "id", None)
    snap = next((s for s in request.chain_snapshot if int(s["position"]) == request.current_position), {})
    if str((snap.get("org") or [None, None])[1]) != str(getattr(principal, "org_id", None)):
        return False
    return "time:approve" in (getattr(principal, "portal_permissions", None) or ())


async def client_timesheet_summaries(
    ctx: RequestContext, db: AsyncSession, request_ids: list[uuid.UUID]
) -> list[ClientTimesheetSummary]:
    """Projection for the Client Portal approvals inbox (Epic 18), by approval request id."""
    rows = list(
        (await db.execute(select(Timesheet).where(Timesheet.approval_request_id.in_(request_ids)))).scalars()
    )
    visible = set(await _visible_ids(ctx, db, [r.id for r in rows])) if rows else set()
    return await _client_summaries(ctx, db, [r for r in rows if r.id in visible])


async def _client_summaries(
    ctx: RequestContext, db: AsyncSession, rows: list[Timesheet]
) -> list[ClientTimesheetSummary]:
    if not rows:
        return []
    base = {s.id: s for s in await ts_service.summaries(db, rows)}
    meta = await _placement_meta(db, {r.placement_id for r in rows})
    req_ids = [r.approval_request_id for r in rows if r.approval_request_id]
    requests = (
        {
            r.id: r
            for r in (
                await db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(req_ids)))
            ).scalars()
        }
        if req_ids
        else {}
    )
    entries: dict[uuid.UUID, list[TimesheetEntry]] = defaultdict(list)
    for e in (
        await db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id.in_([r.id for r in rows])))
    ).scalars():
        entries[e.timesheet_id].append(e)
    out: list[ClientTimesheetSummary] = []
    for r in rows:
        s = base[r.id]
        cfg = await config_version(db, r.placement_id, r.config_version)
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        per_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
        for e in entries[r.id]:
            totals[e.category] += e.hours
            per_day[e.entry_date] += e.hours
        out.append(
            ClientTimesheetSummary(
                id=r.id,
                human_id=r.human_id,
                placement_id=r.placement_id,
                worker_name=s.worker_name,
                role_title=meta.get(r.placement_id, (None, None))[1],
                kind=r.kind,
                period_start=r.period_start,
                period_end=r.period_end,
                status=r.status,
                total_hours=s.total_hours,
                totals={k: v.quantize(TWO) for k, v in totals.items()},
                flags=ClientTimesheetFlags(
                    ot_above_threshold=totals.get("ot", ZERO) > 0 or totals.get("double_time", ZERO) > 0,
                    holiday_entries=totals.get("holiday", ZERO) > 0,
                    above_daily_cap=any(v > Decimal(cfg.daily_cap) for v in per_day.values()),
                ),
                approval_request_id=r.approval_request_id,
                awaiting_client=_awaiting(
                    ctx.principal, requests.get(r.approval_request_id) if r.approval_request_id else None  # nosec B113 - 'requests' is a local dict .get(), not the requests HTTP library
                ),
                submitted_at=r.submitted_at,
            )
        )
    return out


async def client_list(
    ctx: RequestContext, cdb: AsyncSession, tenant_id: uuid.UUID, *, status: str = "pending"
) -> list[ClientTimesheetSummary]:
    ids = await _visible_ids(ctx, cdb)
    if not ids:
        return []
    async with tenant_session(tenant_id) as db:
        stmt = (
            select(Timesheet)
            .where(Timesheet.id.in_(ids))
            .order_by(Timesheet.period_start.desc(), Timesheet.human_id)
        )
        if status == "pending":
            stmt = stmt.where(Timesheet.status.in_(("submitted", "in_approval")))
        else:
            stmt = stmt.where(Timesheet.status != "draft")
        rows = list((await db.execute(stmt.limit(500))).scalars())
        out = await _client_summaries(ctx, db, rows)
    return [s for s in out if s.awaiting_client] if status == "pending" else out


async def client_timesheet_detail(
    ctx: RequestContext, db: AsyncSession, timesheet_id: uuid.UUID
) -> ClientTimesheetDetail:
    """Read-only day x category grid, hours only (FR-80). ``db`` must already be access-checked or org
    scoped."""
    ts = await get_timesheet(db, timesheet_id)
    summary = (await _client_summaries(ctx, db, [ts]))[0]
    days: dict[date, dict[str, Decimal]] = defaultdict(dict)
    comments: dict[date, str] = {}
    for e in await ts_service._entries(db, ts.id):
        days[e.entry_date][e.category] = e.hours
        if e.note and not e.prefilled:
            comments[e.entry_date] = e.note
    deltas: dict[date, dict[str, Decimal]] = defaultdict(dict)
    for a in await ts_service.adjustment_entries(db, ts.id):
        deltas[a.entry_date][a.category] = a.delta_hours
    return ClientTimesheetDetail(
        **summary.model_dump(),
        days=[
            ClientTimesheetDay(
                date=d, hours=h, total=sum(h.values(), ZERO).quantize(TWO), day_comment=comments.get(d)
            )
            for d, h in sorted(days.items())
        ],
        adjustment_deltas=[
            ClientTimesheetDay(date=d, hours=h, total=sum(h.values(), ZERO).quantize(TWO), day_comment=None)
            for d, h in sorted(deltas.items())
        ],
        rejection_comment=ts.rejection_comment,
    )


async def client_detail(
    ctx: RequestContext, cdb: AsyncSession, tenant_id: uuid.UUID, timesheet_id: uuid.UUID
) -> ClientTimesheetDetail:
    if timesheet_id not in await _visible_ids(ctx, cdb, [timesheet_id]):
        raise _not_found()
    async with tenant_session(tenant_id) as db:
        return await client_timesheet_detail(ctx, db, timesheet_id)


async def link_summary(
    ctx: RequestContext, db: AsyncSession, timesheet_id: uuid.UUID
) -> ClientTimesheetSummary:
    """Secure Link / hub projection (Epic 18)."""
    ts = await get_timesheet(db, timesheet_id)
    return (await _client_summaries(ctx, db, [ts]))[0]


async def client_decide(
    ctx: RequestContext,
    cdb: AsyncSession,
    tenant_id: uuid.UUID,
    timesheet_id: uuid.UUID,
    action: str,
    comment: str | None,
) -> ClientTimesheetDetail:
    if timesheet_id not in await _visible_ids(ctx, cdb, [timesheet_id]):
        raise _not_found()
    if action == "reject" and not (comment or "").strip():
        raise ValidationFailed("A comment is required", code="comment_required")
    principal = ctx.principal
    actor = ActorRef("client_user", getattr(principal, "id", None))
    async with tenant_session(tenant_id) as db:
        ts = await get_timesheet(db, timesheet_id, lock=True)
        await decide_as(ctx, db, ts, action, comment, actor)
        await db.refresh(ts)
        return await client_timesheet_detail(ctx, db, timesheet_id)


def _preview_hash(items: list[ClientTimesheetSummary]) -> str:
    raw = "|".join(f"{i.id}:{i.status}:{i.total_hours}" for i in sorted(items, key=lambda x: str(x.id)))
    return hashlib.sha256(raw.encode()).hexdigest()


async def bulk_preview(
    ctx: RequestContext, cdb: AsyncSession, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> ClientBulkPreviewOut:
    visible = set(await _visible_ids(ctx, cdb, ids))
    async with tenant_session(tenant_id) as db:
        rows = (
            list((await db.execute(select(Timesheet).where(Timesheet.id.in_(visible)))).scalars())
            if visible
            else []
        )
        summaries = await _client_summaries(ctx, db, rows)
    items = [s for s in summaries if s.awaiting_client]
    decidable = {i.id for i in items}
    return ClientBulkPreviewOut(
        items=items,
        not_decidable=[i for i in ids if i not in decidable],
        total_hours=sum((i.total_hours for i in items), ZERO).quantize(TWO),
        preview_hash=_preview_hash(items),
    )


async def bulk_decide(
    ctx: RequestContext,
    cdb: AsyncSession,
    tenant_id: uuid.UUID,
    ids: list[uuid.UUID],
    action: str,
    comment: str | None,
    preview_hash: str,
) -> ClientBulkDecideOut:
    """Decide every previewed item. Re-sending the same request is safe: items no longer awaiting this user
    are
    reported as ``skipped`` (``not_pending``) and nothing is decided twice."""
    if action == "reject" and not (comment or "").strip():
        raise ValidationFailed("A comment is required", code="comment_required")
    preview = await bulk_preview(ctx, cdb, tenant_id, ids)
    decided: list[uuid.UUID] = []
    skipped = [ClientBulkSkip(timesheet_id=i, code="not_pending") for i in preview.not_decidable]
    if preview.items and preview.preview_hash != preview_hash:
        raise ConflictError("The selection changed; review it again", code="preview_stale")
    actor = ActorRef("client_user", getattr(ctx.principal, "id", None))
    for item in preview.items:
        try:
            async with tenant_session(tenant_id) as db:
                ts = await get_timesheet(db, item.id, lock=True)
                await decide_as(ctx, db, ts, action, comment, actor)
            decided.append(item.id)
        except (ConflictError, ValidationFailed) as exc:
            skipped.append(ClientBulkSkip(timesheet_id=item.id, code=exc.code))
    return ClientBulkDecideOut(decided=decided, skipped=skipped)
