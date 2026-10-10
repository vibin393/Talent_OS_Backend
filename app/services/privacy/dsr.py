"""Data subject requests and legal holds (Epic 12 story 12.2, FR-68, AD-4).

``DSR_TRANSITIONS`` is the only way ``dsr_requests.status`` changes (``transition``):

| From                       | To          | Actors          | Condition                        |
|----------------------------|-------------|-----------------|----------------------------------|
| received                   | in_progress | user            |                                  |
| received / in_progress     | refused     | user            | ``refusal_reason`` >= 10 chars   |
| in_progress                | completed   | user, system    |                                  |

``execute`` does one thing per kind: access -> subject export + secure link; correction -> 409
``use_candidate_edit``; consent_withdrawal -> withdraw purposes; erasure -> erasure run (requires verified
identity and no active legal hold). Access and erasure both require a verified identity (data leaves or is
destroyed); portal requests are verified on creation.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.core.sequences import next_human_id
from app.core.settings import get_settings
from app.events.privacy import DsrCompleted, SubjectExportRequested
from app.models.privacy import DsrRequest, ErasureRun, LegalHold
from app.schemas.privacy import (
    DsrCreate,
    DsrDetailOut,
    DsrExecuteIn,
    DsrFilters,
    DsrOut,
    DsrUpdate,
    ErasureRunOut,
    LegalHoldCreate,
    LegalHoldOut,
    TimelineEntry,
)
from app.services import settings_service
from app.services.candidates import privacy_port as candidates
from app.services.privacy import consent
from app.services.privacy.common import actor_id, now, require_tenant

DSR_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("received", "in_progress"): frozenset({"user"}),
    ("received", "refused"): frozenset({"user"}),
    ("in_progress", "refused"): frozenset({"user"}),
    ("in_progress", "completed"): frozenset({"user", "system"}),
}
OPEN_STATUSES = ("received", "in_progress")
MIN_REFUSAL_REASON = 10
DUE_SOON = timedelta(days=7)


# ================================================================ helpers
def is_overdue(row: DsrRequest, at: datetime | None = None) -> bool:
    return row.status in OPEN_STATUSES and (at or now()) > row.due_at


async def _get(db: AsyncSession, dsr_id: uuid.UUID, *, for_update: bool = False) -> DsrRequest:
    row = await db.get(DsrRequest, dsr_id, with_for_update=for_update)
    if row is None:
        raise NotFound("Data subject request not found")
    return row


def _out(row: DsrRequest, human_ids: dict[uuid.UUID, str] | None = None) -> DsrOut:
    return DsrOut(
        id=row.id,
        human_id=row.human_id,
        candidate_id=row.candidate_id,
        subject_kind=row.subject_kind,  # type: ignore[arg-type]
        subject_id=row.subject_id,
        candidate_human_id=(human_ids or {}).get(row.candidate_id) if row.candidate_id else None,
        requester_name=row.requester_name,
        requester_email=row.requester_email,
        kind=row.kind,  # type: ignore[arg-type]
        channel=row.channel,  # type: ignore[arg-type]
        details=row.details or {},
        status=row.status,  # type: ignore[arg-type]
        received_at=row.received_at,
        due_at=row.due_at,
        overdue=is_overdue(row),
        assigned_to=row.assigned_to,
        identity_verified_at=row.identity_verified_at,
        identity_verified_by=row.identity_verified_by,
        refusal_reason=row.refusal_reason,
        completed_at=row.completed_at,
        completed_by=row.completed_by,
        result=row.result,
        created_at=row.created_at,
    )


async def _human_ids(db: AsyncSession, rows: list[DsrRequest]) -> dict[uuid.UUID, str]:
    facts = await candidates.basics(db, [r.candidate_id for r in rows if r.candidate_id])
    return {cid: f["human_id"] for cid, f in facts.items()}


async def _require_candidate(db: AsyncSession, candidate_id: uuid.UUID | None) -> None:
    if candidate_id is not None and not await candidates.exists(db, candidate_id):
        raise NotFound("Candidate not found")


# ================================================================ CRUD
async def create_dsr(
    ctx: RequestContext, db: AsyncSession, data: DsrCreate, *, channel: str | None = None
) -> DsrOut:
    """Record a DSR. Portal requests (Epic 9) pass ``channel="candidate_portal"``: identity counts as verified."""
    tenant_id = require_tenant(ctx)
    await _require_candidate(db, data.candidate_id)
    # Epic 35 (A3): non-candidate subjects are internal intake only and name their subject id.
    subject_kind = data.subject_kind
    subject_id = data.subject_id
    if subject_kind == "candidate":
        subject_id = data.candidate_id
    else:
        if channel == "candidate_portal":
            raise ValidationFailed("Portal DSRs are about the candidate", details={"fields": {"subject_kind": "candidate only"}})
        if subject_id is None:
            raise ValidationFailed("subject_id is required", details={"fields": {"subject_id": "Required"}})
    if data.candidate_id is None and subject_id is None and not (data.requester_email or data.requester_name):
        raise ValidationFailed(
            "A candidate or the requester's contact is required",
            details={"fields": {"candidate_id": "Link a candidate or give the requester's name/email"}},
        )
    received = data.received_at or now()
    resolved_channel = channel or data.channel
    verified = resolved_channel == "candidate_portal"
    row = DsrRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        human_id=await next_human_id(db, "DSR", prefix=await settings_service.id_prefix(db, "DSR")),
        candidate_id=data.candidate_id if subject_kind == "candidate" else None,
        subject_kind=subject_kind,
        subject_id=subject_id,
        requester_name=data.requester_name,
        requester_email=str(data.requester_email) if data.requester_email else None,
        kind=data.kind,
        channel=resolved_channel,
        details=data.details,
        status="received",
        received_at=received,
        due_at=received + timedelta(days=get_settings().dsr_due_days),
        identity_verified_at=now() if verified else None,
    )
    db.add(row)
    await db.flush()
    audit.record("dsr.received", "dsr_request", row.id, session=db, kind=row.kind, channel=row.channel)
    return _out(row, await _human_ids(db, [row]))


def _filtered(filters: DsrFilters) -> Select[tuple[DsrRequest]]:
    stmt = select(DsrRequest)
    if filters.status is not None:
        stmt = stmt.where(DsrRequest.status == filters.status)
    if filters.kind is not None:
        stmt = stmt.where(DsrRequest.kind == filters.kind)
    if filters.assigned_to is not None:
        stmt = stmt.where(DsrRequest.assigned_to == filters.assigned_to)
    if filters.overdue is not None:
        overdue = and_(DsrRequest.status.in_(OPEN_STATUSES), DsrRequest.due_at < func.now())
        stmt = stmt.where(overdue if filters.overdue else not_(overdue))
    return stmt.order_by(DsrRequest.due_at.asc(), DsrRequest.id)


async def list_dsrs(ctx: RequestContext, db: AsyncSession, filters: DsrFilters, params: PageParams) -> Page[DsrOut]:  # noqa: ARG001
    page = await paginate(db, _filtered(filters), params)
    rows = list(page.items)
    ids = await _human_ids(db, rows)
    return Page[DsrOut](items=[_out(r, ids) for r in rows], total=page.total, page=page.page, page_size=page.page_size)


async def _timeline(db: AsyncSession, row: DsrRequest) -> list[TimelineEntry]:
    entries = await audit.search(
        db, audit.AuditFilters(entity_type="dsr_request", entity_id=str(row.id)), PageParams(page=1, page_size=100)
    )
    return [
        TimelineEntry(
            occurred_at=e.occurred_at, action=e.action, actor_type=e.actor_type, actor_id=e.actor_id, data=e.data
        )
        for e in reversed(entries.items)
    ]


async def get_dsr(ctx: RequestContext, db: AsyncSession, dsr_id: uuid.UUID) -> DsrDetailOut:  # noqa: ARG001
    row = await _get(db, dsr_id)
    run = (
        await db.execute(
            select(ErasureRun).where(ErasureRun.dsr_id == row.id).order_by(ErasureRun.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    hold = await active_hold(db, row.candidate_id) if row.candidate_id else None
    base = _out(row, await _human_ids(db, [row]))
    return DsrDetailOut(
        **base.model_dump(),
        timeline=await _timeline(db, row),
        erasure_run=ErasureRunOut.model_validate(run) if run is not None else None,
        legal_hold=LegalHoldOut.model_validate(hold) if hold is not None else None,
    )


async def update_dsr(ctx: RequestContext, db: AsyncSession, dsr_id: uuid.UUID, data: DsrUpdate) -> DsrDetailOut:
    row = await _get(db, dsr_id, for_update=True)
    if row.status not in OPEN_STATUSES:
        raise ConflictError("The request is closed", code="dsr_closed")
    fields = data.model_fields_set
    if "candidate_id" in fields:
        await _require_candidate(db, data.candidate_id)
        row.candidate_id = data.candidate_id
        if row.subject_kind == "candidate":
            row.subject_id = data.candidate_id
    if "assigned_to" in fields:
        if data.assigned_to is not None:
            from app.services import identity_bridge

            processors = {u["id"] for u in await identity_bridge.users_with_permission(db, "dsr:process")}
            if data.assigned_to not in processors:
                raise ValidationFailed(
                    "Assignee cannot process DSRs",
                    details={"fields": {"assigned_to": "User must hold dsr:process"}},
                )
        row.assigned_to = data.assigned_to
    if "details" in fields and data.details is not None:
        row.details = data.details
    await db.flush()
    return await get_dsr(ctx, db, dsr_id)


async def verify_identity(ctx: RequestContext, db: AsyncSession, dsr_id: uuid.UUID) -> DsrDetailOut:
    row = await _get(db, dsr_id, for_update=True)
    if row.status not in OPEN_STATUSES:
        raise ConflictError("The request is closed", code="dsr_closed")
    if row.identity_verified_at is None:
        row.identity_verified_at = now()
        row.identity_verified_by = actor_id(ctx)
        await db.flush()
        audit.record("dsr.identity_verified", "dsr_request", row.id, session=db)
    return await get_dsr(ctx, db, dsr_id)


async def _transition(
    db: AsyncSession,
    row: DsrRequest,
    to: str,
    *,
    actor: str,
    actor_user: uuid.UUID | None,
    refusal_reason: str | None = None,
    note: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    allowed = DSR_TRANSITIONS.get((row.status, to))
    if allowed is None or actor not in allowed:
        raise ConflictError(
            f"Cannot move a {row.status} request to {to}",
            code="invalid_transition",
            details={"from": row.status, "to": to, "actor": actor},
        )
    if to == "refused":
        if not refusal_reason or len(refusal_reason.strip()) < MIN_REFUSAL_REASON:
            raise ValidationFailed(
                "A refusal reason of at least 10 characters is required",
                details={"fields": {"refusal_reason": "At least 10 characters"}},
            )
        row.refusal_reason = refusal_reason.strip()
    old = row.status
    row.status = to
    if to == "completed":
        row.completed_at = now()
        row.completed_by = actor_user
        row.result = {**(row.result or {}), **(result or {})}
    await db.flush()
    audit.record(
        "dsr.transitioned", "dsr_request", row.id, session=db, **{"from": old, "to": to, "actor_kind": actor, "note": note}
    )
    if to == "completed":
        emit(db, DsrCompleted(tenant_id=row.tenant_id, dsr_id=row.id, kind=row.kind))


async def transition(
    ctx: RequestContext,
    db: AsyncSession,
    dsr_id: uuid.UUID,
    to: str,
    *,
    refusal_reason: str | None = None,
    note: str | None = None,
) -> DsrDetailOut:
    row = await _get(db, dsr_id, for_update=True)
    await _transition(db, row, to, actor="user", actor_user=actor_id(ctx), refusal_reason=refusal_reason, note=note)
    return await get_dsr(ctx, db, dsr_id)


async def complete_system(db: AsyncSession, dsr_id: uuid.UUID, result: dict[str, Any]) -> None:
    """Complete a DSR from a job (erasure / export). Idempotent: already-completed requests are left alone."""
    row = await db.get(DsrRequest, dsr_id, with_for_update=True)
    if row is None or row.status == "completed":
        return
    if row.status == "received":  # defensive: execute always moves it to in_progress first
        row.status = "in_progress"
        await db.flush()
    await _transition(db, row, "completed", actor="system", actor_user=None, result=result)


# ================================================================ execute
async def execute(ctx: RequestContext, db: AsyncSession, dsr_id: uuid.UUID, data: DsrExecuteIn) -> DsrDetailOut:
    row = await _get(db, dsr_id, for_update=True)
    if row.status not in OPEN_STATUSES:
        raise ConflictError("The request is closed", code="dsr_closed")
    if row.kind == "correction":
        raise ConflictError(
            "Edit the candidate, then complete the request with a note", code="use_candidate_edit"
        )
    if row.candidate_id is None:
        raise ConflictError("Link the request to a candidate first", code="dsr_no_candidate")
    if row.kind in ("access", "erasure") and row.identity_verified_at is None:
        raise ConflictError("Verify the requester's identity first", code="identity_not_verified")
    if row.kind == "erasure":
        hold = await active_hold(db, row.candidate_id)
        if hold is not None:
            raise ConflictError(
                "The candidate is under a legal hold", code="legal_hold_active", details={"reason": hold.reason}
            )
        if (data.confirm or "").strip().upper() not in ("ERASE", ""):
            raise ValidationFailed("Type ERASE to confirm", details={"fields": {"confirm": "Type ERASE"}})
    user = actor_id(ctx)
    if row.status == "received":
        await _transition(db, row, "in_progress", actor="user", actor_user=user)
    audit.record("dsr.executed", "dsr_request", row.id, session=db, kind=row.kind)
    if row.kind == "consent_withdrawal":
        purposes = data.purposes or await consent.granted_purposes(db, row.candidate_id)
        if consent.ENGAGEMENT_PURPOSE in purposes and await consent.has_active_engagement(db, row.candidate_id):
            # Epic 35 (FR-179): engagement is legitimate use while on assignment -- kept, reported in the result.
            purposes = [p for p in purposes if p != consent.ENGAGEMENT_PURPOSE]
        withdrawn = {}
        for purpose in purposes:
            withdrawn[purpose] = await consent.withdraw_consent(
                ctx, db, row.candidate_id, purpose, by_kind="candidate"
            )
        await _transition(
            db, row, "completed", actor="user", actor_user=user, result={"withdrawn": withdrawn, "purposes": purposes}
        )
    elif row.kind == "access":
        emit(db, SubjectExportRequested(tenant_id=row.tenant_id, dsr_id=row.id))
    elif row.kind == "erasure":
        from app.services.privacy import erasure

        await erasure.create_run(ctx, db, row.candidate_id, reason="dsr", dsr_id=row.id)
    await db.flush()
    return await get_dsr(ctx, db, dsr_id)


# ================================================================ due-date alerts
async def due_scan(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    """Daily: one "due soon" notice 7 days before ``due_at`` and one "overdue" notice (idempotent)."""
    from app.services import identity_bridge
    from app.services.privacy.common import email_users

    at = now()
    rows = list(
        (await db.execute(select(DsrRequest).where(DsrRequest.status.in_(OPEN_STATUSES)).with_for_update())).scalars()
    )
    due_soon = [r for r in rows if r.due_soon_notified_at is None and r.due_at - DUE_SOON <= at <= r.due_at]
    overdue = [r for r in rows if r.overdue_notified_at is None and at > r.due_at]
    if not due_soon and not overdue:
        return {"due_soon": 0, "overdue": 0}
    recipients = await identity_bridge.users_with_permission(db, "dsr:process")
    for r in due_soon:
        r.due_soon_notified_at = at
        audit.record("dsr.due_soon_notified", "dsr_request", r.id, session=db, actor_type="system", actor_id=None)
    for r in overdue:
        r.overdue_notified_at = at
        audit.record("dsr.overdue_notified", "dsr_request", r.id, session=db, actor_type="system", actor_id=None)
    await db.flush()
    lines = [f"- {r.human_id} ({r.kind}) due {r.due_at:%Y-%m-%d}: OVERDUE" for r in overdue]
    lines += [f"- {r.human_id} ({r.kind}) due {r.due_at:%Y-%m-%d}" for r in due_soon]
    await email_users(
        recipients,
        "Data subject requests need attention",
        "The following data subject requests are due soon or overdue:\n\n" + "\n".join(lines) + "\n",
        tenant_id=tenant_id,
        kind="dsr_due_alert",
    )
    return {"due_soon": len(due_soon), "overdue": len(overdue)}


# ================================================================ legal holds
async def active_hold(db: AsyncSession, candidate_id: uuid.UUID) -> LegalHold | None:
    stmt = (
        select(LegalHold)
        .where(LegalHold.candidate_id == candidate_id, LegalHold.released_at.is_(None))
        .order_by(LegalHold.placed_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def held_candidates(db: AsyncSession) -> set[uuid.UUID]:
    rows = await db.execute(select(LegalHold.candidate_id).where(LegalHold.released_at.is_(None)))
    return set(rows.scalars())


async def open_dsr_candidates(db: AsyncSession) -> set[uuid.UUID]:
    rows = await db.execute(
        select(DsrRequest.candidate_id).where(
            DsrRequest.status.in_(OPEN_STATUSES), DsrRequest.candidate_id.is_not(None)
        )
    )
    return set(rows.scalars())


async def list_holds(ctx: RequestContext, db: AsyncSession, active_only: bool = True) -> list[LegalHoldOut]:  # noqa: ARG001
    stmt = select(LegalHold)
    if active_only:
        stmt = stmt.where(LegalHold.released_at.is_(None))
    rows = (await db.execute(stmt.order_by(LegalHold.placed_at.desc()))).scalars()
    return [LegalHoldOut.model_validate(r) for r in rows]


async def place_hold(ctx: RequestContext, db: AsyncSession, data: LegalHoldCreate) -> LegalHoldOut:
    tenant_id = require_tenant(ctx)
    await _require_candidate(db, data.candidate_id)
    row = LegalHold(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        candidate_id=data.candidate_id,
        reason=data.reason.strip(),
        placed_by=actor_id(ctx),
        placed_at=now(),
    )
    db.add(row)
    await db.flush()
    return LegalHoldOut.model_validate(row)


async def release_hold(ctx: RequestContext, db: AsyncSession, hold_id: uuid.UUID) -> LegalHoldOut:
    row = await db.get(LegalHold, hold_id, with_for_update=True)
    if row is None:
        raise NotFound("Legal hold not found")
    if row.released_at is None:
        row.released_at = now()
        row.released_by = actor_id(ctx)
        await db.flush()
    return LegalHoldOut.model_validate(row)
