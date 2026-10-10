"""Timesheet approval on the extended approvals engine (FR-130, AD-24).

* ``start_route`` resolves ``placement_time_route`` (AM -> Client -> Finance by default) and starts the
request;
  an empty route auto-approves (actor ``system``) only when the tenant allows it.
* ``on_step_decided`` / ``on_final_decided`` run synchronously in the deciding transaction with the deciding
  ``ActorRef``: non-final approve -> ``in_approval``; final approve -> ``approved``; reject -> ``rejected`` (+
  Worker notification with the comment through ``TimesheetRejected``).
* ``record_client_approval``: an offline Client approval recorded by a User with scanned evidence
  (actor ``recorded_by_user``).
"""

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.events.time import TimesheetApproved, TimesheetRejected
from app.models.approvals import ApprovalRequest
from app.models.time import Timesheet, TimesheetAdjustmentEntry, TimesheetEntry
from app.schemas.approvals import DecideIn, SubjectSummary
from app.services import approvals
from app.services.approvals import FinalDecision, StepDecision
from app.services.time.common import get_timesheet, now, settings_row
from app.services.time.events import TimesheetStepDecided
from app.services.time.timesheets import collapse_split, transition_timesheet

SUBJECT = "timesheet"
ROUTE = "placement_time_route"
CLIENT_KINDS = ("client_user", "client_any")


async def _facts(db: AsyncSession, ts: Timesheet) -> dict[str, Any]:
    hours: dict[str, Any] = defaultdict(lambda: 0)
    if ts.kind == "adjustment":
        rows = await db.execute(
            select(TimesheetAdjustmentEntry.category, TimesheetAdjustmentEntry.delta_hours).where(
                TimesheetAdjustmentEntry.timesheet_id == ts.id
            )
        )
    else:
        rows = await db.execute(
            select(TimesheetEntry.category, TimesheetEntry.hours).where(TimesheetEntry.timesheet_id == ts.id)
        )
    for cat, h in rows.all():
        hours[cat] += h
    return {
        "timesheet_id": str(ts.id),
        "placement_id": str(ts.placement_id),
        "client_id": str(ts.client_id),
        "kind": ts.kind,
        "period_start": ts.period_start.isoformat(),
        "period_end": ts.period_end.isoformat(),
        "hours": {k: str(v) for k, v in hours.items()},
    }


async def _approve(db: AsyncSession, ts: Timesheet, actor: ActorRef, *, auto: bool = False) -> None:
    transition_timesheet(ts, "approved", actor, auto=auto)
    ts.approved_at = now()
    await db.flush()
    emit(db, TimesheetApproved(tenant_id=ts.tenant_id, timesheet_id=ts.id, placement_id=ts.placement_id))


async def start_route(ctx: RequestContext, db: AsyncSession, ts: Timesheet, actor: ActorRef) -> None:
    steps = await approvals.resolve_route(ctx, db, ROUTE, placement_id=ts.placement_id, include_client=True)
    if not steps:
        if not (await settings_row(db, ts.tenant_id)).allow_empty_route:
            raise ValidationFailed(
                "This placement has no timesheet approval route", code="approval_route_empty"
            )
        await _approve(db, ts, ActorRef("system", None), auto=True)
        return
    result = await approvals.start(
        ctx, db, SUBJECT, ts.id, await _facts(db, ts), steps=steps, initiated_by=actor
    )
    ts.approval_request_id = result.request_id
    await db.flush()
    if result.status == "auto_approved" and ts.status == "submitted":
        await _approve(db, ts, ActorRef("system", None), auto=True)


async def on_step_decided(ctx: RequestContext, db: AsyncSession, decision: StepDecision) -> None:  # noqa: ARG001
    ts = await get_timesheet(db, decision.subject_id, lock=True)
    if decision.outcome == "approved" and not decision.is_final and ts.status == "submitted":
        transition_timesheet(ts, "in_approval", decision.actor)
    elif decision.outcome != "approved":
        ts.rejected_step = decision.step_name[:100]
    await db.flush()
    emit(
        db,
        TimesheetStepDecided(
            tenant_id=ts.tenant_id,
            timesheet_id=ts.id,
            placement_id=ts.placement_id,
            step=decision.step_name,
            decision=decision.outcome,
            actor_kind=decision.actor.kind,
        ),
    )


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: FinalDecision) -> None:  # noqa: ARG001
    ts = await get_timesheet(db, decision.subject_id, lock=True)
    if ts.status not in ("submitted", "in_approval"):
        return
    if decision.outcome in ("approved", "auto_approved"):
        actor = decision.actor or ActorRef("system", None)
        await _approve(db, ts, actor, auto=decision.actor is None)
        return
    transition_timesheet(ts, "rejected", decision.actor or ActorRef("system", None))
    ts.rejection_comment = decision.comment
    await db.flush()
    await collapse_split(db, ts)
    emit(
        db,
        TimesheetRejected(
            tenant_id=ts.tenant_id,
            timesheet_id=ts.id,
            placement_id=ts.placement_id,
            step_name=ts.rejected_step or "",
        ),
    )


async def describe(
    ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, SubjectSummary]:
    del ctx
    from app.models.candidates import Candidate

    rows = (
        await db.execute(
            select(
                Timesheet.id,
                Timesheet.human_id,
                Timesheet.period_start,
                Timesheet.period_end,
                Candidate.full_name,
            )
            .join(Candidate, Candidate.id == Timesheet.candidate_id, isouter=True)
            .where(Timesheet.id.in_(ids))
        )
    ).all()
    return {
        r.id: SubjectSummary(
            label=f"Timesheet {r.human_id}",
            title=f"{r.full_name or 'Worker'} · {r.period_start.isoformat()} to {r.period_end.isoformat()}",
        )
        for r in rows
    }


async def current_request(db: AsyncSession, ts: Timesheet) -> ApprovalRequest | None:
    if ts.approval_request_id is None:
        return None
    return (
        await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == ts.approval_request_id))
    ).scalar_one_or_none()


async def _client_user_for(db: AsyncSession, client_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.external_auth import ClientUser

    rows = (
        await db.execute(
            select(ClientUser.id, ClientUser.portal_permissions)
            .where(ClientUser.client_id == client_id, ClientUser.status == "active")
            .order_by(ClientUser.created_at)
        )
    ).all()
    for cid, perms in rows:
        if "time:approve" in (perms or []):
            return uuid.UUID(str(cid))
    return None


async def record_client_approval(
    ctx: RequestContext,
    db: AsyncSession,
    timesheet_id: uuid.UUID,
    *,
    data: bytes,
    filename: str | None,
    client_user_id: uuid.UUID | None,
    comment: str | None,
) -> Timesheet:
    from app.services import files_service

    ts = await get_timesheet(db, timesheet_id, lock=True)
    request = await current_request(db, ts)
    if ts.status not in ("submitted", "in_approval") or request is None or request.status != "pending":
        raise ConflictError("This timesheet is not waiting for approval", code="client_step_not_current")
    if request.current_approver_kind not in CLIENT_KINDS:
        raise ConflictError("The current step is not the Client step", code="client_step_not_current")
    if request.current_approver_kind == "client_user":
        attributed = request.current_approver_client_user_id
    else:
        attributed = client_user_id or await _client_user_for(db, ts.client_id)
    if attributed is None:
        raise ValidationFailed("Choose the Client User who approved", code="client_user_required")
    stored = await files_service.store_upload(
        ctx, db, data, filename, frozenset({"pdf", "png", "jpeg"}), "timesheet", ts.id
    )
    principal_id = getattr(ctx.principal, "id", None)
    actor = ActorRef(
        "recorded_by_user",
        principal_id,
        attributed_to=("client_user", attributed),
        evidence_file_id=stored.id,
    )
    ts.client_approval_evidence_file_id = stored.id
    await db.flush()
    await approvals.decide(ctx, db, request.id, DecideIn(action="approve", comment=comment), actor=actor)
    return ts


async def decide_as(
    ctx: RequestContext,
    db: AsyncSession,
    ts: Timesheet,
    action: str,
    comment: str | None,
    actor: ActorRef | None = None,
) -> None:
    """Decide the current step of a Timesheet's request as ``actor`` (Client Portal / internal)."""
    if ts.approval_request_id is None or ts.status not in ("submitted", "in_approval"):
        raise ConflictError("This timesheet is not waiting for approval", code="request_not_pending")
    await approvals.decide(
        ctx,
        db,
        ts.approval_request_id,
        DecideIn(action=action, comment=comment),
        actor=actor or actor_from_ctx(ctx),  # type: ignore[arg-type]
    )
