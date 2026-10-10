# ruff: noqa: E501
"""Deliverables and their acceptance (Epic 45, FR-228, PRD 5.7).

AD-4 transitions (``TRANSITIONS``): ``planned -> in_progress`` (start), ``in_progress -> submitted`` (submit),
``submitted -> accepted`` (★ accept), ``submitted -> in_progress`` (reject; ``rejected`` is recorded on the event
only), ``planned | in_progress -> cancelled``. Anything else is 409 ``transition_not_allowed``.

★ Acceptance (AD-24 / FR-257): only ``client_user`` (Client Portal, ``deliverables:accept``) or
``recorded_by_user`` (a User with ``deliverable:record`` and an evidence file) -- never ``system`` / ``workflow``
/ ``ai`` / ``api_key`` (403 ``human_only_action``). The row is locked; replaying an acceptance returns the current
state (one event row, one milestone transition). When every non-cancelled deliverable of the milestone is accepted,
``commercials_service.mark_milestone_billable`` runs in the same transaction with the same ``ActorRef``.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import ActorRef, actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound
from app.core.events import emit
from app.events.projects import DeliverableAccepted, DeliverableRejected, DeliverableSubmitted
from app.models.commercials import ContractMilestone
from app.models.projects import Deliverable, DeliverableEvent, Project
from app.schemas.projects import DeliverableEventOut, DeliverableIn, DeliverableOut, DeliverableUpdate
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
)

ENTITY = "deliverable"
EVIDENCE_KINDS = ("pdf", "png", "jpeg", "docx")
ACCEPTOR_KINDS = frozenset({"client_user", "recorded_by_user"})
# (from, to) -> event kind
TRANSITIONS: dict[tuple[str, str], str] = {
    ("planned", "in_progress"): "started",
    ("in_progress", "submitted"): "submitted",
    ("submitted", "accepted"): "accepted",
    ("submitted", "in_progress"): "rejected",
    ("planned", "cancelled"): "cancelled",
    ("in_progress", "cancelled"): "cancelled",
}
EDITABLE = frozenset({"planned", "in_progress"})


def _check(from_status: str, to_status: str) -> str:
    kind = TRANSITIONS.get((from_status, to_status))
    if kind is None:
        raise conflict(
            "transition_not_allowed",
            f"A deliverable cannot move from {from_status} to {to_status}",
            from_status=from_status,
            to_status=to_status,
        )
    return kind


# ================================================================ output
async def outs(
    db: AsyncSession, rows: Sequence[Deliverable], *, with_events: bool = False
) -> list[DeliverableOut]:
    mids = {r.milestone_id for r in rows if r.milestone_id}
    names = (
        dict(
            (
                await db.execute(
                    select(ContractMilestone.id, ContractMilestone.name).where(ContractMilestone.id.in_(mids))
                )
            ).all()
        )
        if mids
        else {}
    )
    events: dict[uuid.UUID, list[DeliverableEventOut]] = {}
    if with_events and rows:
        for e in (
            await db.execute(
                select(DeliverableEvent)
                .where(DeliverableEvent.deliverable_id.in_([r.id for r in rows]))
                .order_by(DeliverableEvent.at, DeliverableEvent.id)
            )
        ).scalars():
            events.setdefault(e.deliverable_id, []).append(
                DeliverableEventOut(
                    id=e.id,
                    kind=e.kind,
                    actor=e.actor,
                    reason=e.reason,
                    evidence_file_id=e.evidence_file_id,
                    at=e.at,
                )
            )
    return [
        DeliverableOut(
            id=r.id,
            project_id=r.project_id,
            milestone_id=r.milestone_id,
            milestone_name=names.get(r.milestone_id) if r.milestone_id else None,
            name=r.name,
            description=r.description,
            acceptance_criteria=r.acceptance_criteria,
            due_date=r.due_date,
            status=r.status,  # type: ignore[arg-type]
            submitted_at=r.submitted_at,
            accepted_at=r.accepted_at,
            accepted_actor=r.accepted_actor,
            last_rejection_reason=r.last_rejection_reason,
            evidence_file_ids=list(r.evidence_file_ids or []),
            lock_version=r.lock_version,
            events=events.get(r.id, []),
        )
        for r in rows
    ]


# ================================================================ reads
async def _get(
    ctx: RequestContext,
    db: AsyncSession,
    deliverable_id: uuid.UUID,
    *,
    permission: str = READ,
    lock: bool = False,
) -> tuple[Deliverable, Project]:
    """Return the deliverable of a project in the caller's data scope (404 otherwise)."""
    stmt = scoped(
        ctx, select(Deliverable, Project).join(Project, Project.id == Deliverable.project_id), permission
    ).where(Deliverable.id == deliverable_id)
    if lock:
        stmt = stmt.with_for_update(of=Deliverable)
    found = (await db.execute(stmt)).first()
    if found is None:
        raise NotFound("Deliverable not found")
    return found[0], found[1]


async def list_for_project(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID
) -> list[DeliverableOut]:
    await get_project(ctx, db, project_id)
    rows = list(
        (
            await db.execute(
                select(Deliverable)
                .where(Deliverable.project_id == project_id)
                .order_by(Deliverable.due_date.nulls_last(), Deliverable.created_at)
            )
        ).scalars()
    )
    return await outs(db, rows)


async def get_deliverable(ctx: RequestContext, db: AsyncSession, deliverable_id: uuid.UUID) -> DeliverableOut:
    row, _ = await _get(ctx, db, deliverable_id)
    return (await outs(db, [row], with_events=True))[0]


# ================================================================ writes
async def _check_milestone(db: AsyncSession, project: Project, milestone_id: uuid.UUID | None) -> None:
    if milestone_id is None:
        return
    contract_id = (
        await db.execute(select(ContractMilestone.contract_id).where(ContractMilestone.id == milestone_id))
    ).scalar_one_or_none()
    if contract_id is None or project.contract_id is None or contract_id != project.contract_id:
        raise invalid(
            "milestone_not_in_sow", "The milestone belongs to another contract", field="milestone_id"
        )


def _event(
    row: Deliverable, kind: str, actor: ActorRef, reason: str | None = None, evidence: uuid.UUID | None = None
) -> DeliverableEvent:
    return DeliverableEvent(
        id=uuid.uuid4(),
        tenant_id=row.tenant_id,
        deliverable_id=row.id,
        client_id=row.client_id,
        kind=kind,
        actor=actor.to_json(),
        reason=reason,
        evidence_file_id=evidence,
        at=now(),
    )


async def create(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID, data: DeliverableIn
) -> DeliverableOut:
    project = await get_project(ctx, db, project_id, permission=WRITE)
    if project.status in ("completed", "cancelled"):
        raise conflict(
            "project_closed", "Deliverables are added to open projects only", status=project.status
        )
    await _check_milestone(db, project, data.milestone_id)
    row = Deliverable(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        project_id=project.id,
        client_id=project.client_id,
        milestone_id=data.milestone_id,
        name=data.name,
        description=data.description,
        acceptance_criteria=data.acceptance_criteria,
        due_date=data.due_date,
        status="planned",
    )
    db.add(row)
    await db.flush()
    db.add(_event(row, "created", actor_from_ctx(ctx)))
    await db.flush()
    await timeline(
        ctx,
        db,
        project.id,
        f"{project.human_id}: deliverable {row.name} added",
        {"deliverable_id": str(row.id)},
    )
    await db.refresh(row)
    return (await outs(db, [row], with_events=True))[0]


async def update(
    ctx: RequestContext, db: AsyncSession, deliverable_id: uuid.UUID, data: DeliverableUpdate
) -> DeliverableOut:
    row, project = await _get(ctx, db, deliverable_id, permission=WRITE, lock=True)
    changes = data.model_dump(exclude_unset=True)
    expected = changes.pop("lock_version", None)
    if expected is not None and expected != row.lock_version:
        raise conflict(
            "stale_version", "The deliverable changed meanwhile; reload it", lock_version=row.lock_version
        )
    if row.status not in EDITABLE:
        raise conflict(
            "deliverable_not_editable",
            "Only planned or in-progress deliverables can be edited",
            status=row.status,
        )
    if changes.get("name", "") is None:
        raise invalid("name_required", "The deliverable needs a name", field="name")
    if "milestone_id" in changes:
        await _check_milestone(db, project, changes["milestone_id"])
    for key, value in changes.items():
        setattr(row, key, value)
    row.lock_version += 1
    await db.flush()
    await db.refresh(row)
    return (await outs(db, [row], with_events=True))[0]


async def move(ctx: RequestContext, db: AsyncSession, deliverable_id: uuid.UUID, to: str) -> DeliverableOut:
    """User transitions: start (``in_progress``), submit (``submitted``), cancel (``cancelled``)."""
    row, project = await _get(ctx, db, deliverable_id, permission=WRITE, lock=True)
    if row.status == to:
        return (await outs(db, [row], with_events=True))[0]
    if to not in ("in_progress", "submitted", "cancelled") or (row.status, to) == (
        "submitted",
        "in_progress",
    ):
        raise conflict(
            "transition_not_allowed",
            f"A deliverable cannot move from {row.status} to {to}",
            from_status=row.status,
            to_status=to,
        )
    kind = _check(row.status, to)
    actor = actor_from_ctx(ctx)
    row.status = to
    row.lock_version += 1
    if to == "submitted":
        row.submitted_at = now()
    await db.flush()
    db.add(_event(row, kind, actor))
    await db.flush()
    await timeline(
        ctx,
        db,
        project.id,
        f"{project.human_id}: deliverable {row.name} {kind}",
        {"deliverable_id": str(row.id)},
    )
    if to == "submitted":
        emit(
            db,
            DeliverableSubmitted(
                tenant_id=row.tenant_id,
                deliverable_id=row.id,
                project_id=row.project_id,
                client_id=row.client_id,
            ),
        )
    await db.refresh(row)
    return (await outs(db, [row], with_events=True))[0]


# ================================================================ ★ acceptance / rejection
async def _lock_any(db: AsyncSession, deliverable_id: uuid.UUID) -> Deliverable:
    """Lock by id in the caller's session (the portal session is narrowed by client org RLS)."""
    row = (
        await db.execute(select(Deliverable).where(Deliverable.id == deliverable_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Deliverable not found")
    return row


async def accept(ctx: RequestContext, db: AsyncSession, row: Deliverable, actor: ActorRef) -> Deliverable:
    """★ The single acceptance path (row already locked by the caller)."""
    require_human(actor)
    if actor.kind not in ACCEPTOR_KINDS:
        raise Forbidden("Record the Client's acceptance with its evidence", code="acceptance_actor_invalid")
    if actor.kind == "recorded_by_user" and actor.evidence_file_id is None:
        raise invalid("evidence_required", "Attach the Client's acceptance evidence", field="file")
    if row.status == "accepted":
        return row
    _check(row.status, "accepted")
    row.status = "accepted"
    row.accepted_at = now()
    row.accepted_actor = actor.to_json()
    row.lock_version += 1
    if actor.evidence_file_id is not None:
        row.evidence_file_ids = [*(row.evidence_file_ids or []), actor.evidence_file_id]
    await db.flush()
    db.add(_event(row, "accepted", actor, evidence=actor.evidence_file_id))
    await db.flush()
    await timeline(
        ctx,
        db,
        row.project_id,
        f"Deliverable {row.name} accepted",
        {"deliverable_id": str(row.id), "actor": actor.to_json()},
    )
    audit.record(
        "deliverable.accepted",
        ENTITY,
        row.id,
        session=db,
        project_id=str(row.project_id),
        actor=actor.to_json(),
    )
    emit(
        db,
        DeliverableAccepted(
            tenant_id=row.tenant_id,
            deliverable_id=row.id,
            project_id=row.project_id,
            client_id=row.client_id,
            milestone_id=row.milestone_id,
            actor_kind=actor.kind,
        ),
    )
    if row.milestone_id is not None:
        statuses = (
            await db.execute(
                select(Deliverable.status).where(
                    Deliverable.milestone_id == row.milestone_id, Deliverable.status != "cancelled"
                )
            )
        ).scalars()
        if all(s == "accepted" for s in statuses):
            from app.services import commercials_service

            await commercials_service.mark_milestone_billable(ctx, db, row.milestone_id, actor=actor)
    return row


async def reject(
    ctx: RequestContext, db: AsyncSession, row: Deliverable, actor: ActorRef, reason: str
) -> Deliverable:
    require_human(actor)
    if actor.kind not in ACCEPTOR_KINDS:
        raise Forbidden("Record the Client's rejection", code="acceptance_actor_invalid")
    _check(row.status, "in_progress")
    row.status = "in_progress"
    row.last_rejection_reason = reason
    row.submitted_at = None
    row.lock_version += 1
    await db.flush()
    db.add(_event(row, "rejected", actor, reason=reason))
    await db.flush()
    await timeline(
        ctx,
        db,
        row.project_id,
        f"Deliverable {row.name} rejected",
        {"deliverable_id": str(row.id), "reason": reason[:500]},
    )
    audit.record(
        "deliverable.rejected",
        ENTITY,
        row.id,
        session=db,
        project_id=str(row.project_id),
        actor=actor.to_json(),
    )
    emit(
        db,
        DeliverableRejected(
            tenant_id=row.tenant_id,
            deliverable_id=row.id,
            project_id=row.project_id,
            client_id=row.client_id,
            actor_kind=actor.kind,
        ),
    )
    return row


def _recording_actor(ctx: RequestContext) -> ActorRef:
    """Return the recording User: a person (API keys / workflow / AI: 403 ``human_only_action``)."""
    actor = actor_from_ctx(ctx)
    require_human(actor)
    if actor.kind != "user":
        raise Forbidden("Only a signed-in User records a Client decision", code="human_only_action")
    return actor


async def record_acceptance(
    ctx: RequestContext,
    db: AsyncSession,
    deliverable_id: uuid.UUID,
    *,
    data: bytes | None,
    filename: str | None,
    accepted_by_contact_id: uuid.UUID | None,
) -> DeliverableOut:
    """Record an offline Client acceptance with its evidence (``deliverable:record``)."""
    from app.models.clients import Contact
    from app.services import files_service

    me = _recording_actor(ctx)
    row, project = await _get(ctx, db, deliverable_id, permission=READ, lock=True)
    if row.status == "accepted":
        return (await outs(db, [row], with_events=True))[0]
    if not data:
        raise invalid("evidence_required", "Attach the Client's acceptance evidence", field="file")
    attributed: tuple[str, uuid.UUID] | None = None
    if accepted_by_contact_id is not None:
        contact = (
            await db.execute(select(Contact.client_id).where(Contact.id == accepted_by_contact_id))
        ).scalar_one_or_none()
        if contact is None or contact != project.client_id:
            raise invalid(
                "contact_invalid", "Choose a contact of the project's client", field="accepted_by_contact_id"
            )
        attributed = ("contact", accepted_by_contact_id)
    stored = await files_service.store_upload(ctx, db, data, filename, EVIDENCE_KINDS, ENTITY, row.id)
    actor = ActorRef("recorded_by_user", me.id, attributed_to=attributed, evidence_file_id=stored.id)
    await accept(ctx, db, row, actor)
    await db.refresh(row)
    return (await outs(db, [row], with_events=True))[0]


async def record_rejection(
    ctx: RequestContext, db: AsyncSession, deliverable_id: uuid.UUID, reason: str
) -> DeliverableOut:
    me = _recording_actor(ctx)
    row, _ = await _get(ctx, db, deliverable_id, permission=READ, lock=True)
    await reject(ctx, db, row, ActorRef("recorded_by_user", me.id), reason)
    await db.refresh(row)
    return (await outs(db, [row], with_events=True))[0]


async def portal_decide(
    ctx: RequestContext, db: AsyncSession, deliverable_id: uuid.UUID, outcome: str, reason: str | None = None
) -> Deliverable:
    """Client Portal decision (client org RLS narrows the session; actor = the Client User)."""
    principal: Any = ctx.principal
    actor = ActorRef("client_user", getattr(principal, "id", None))
    row = await _lock_any(db, deliverable_id)
    if outcome == "accept":
        return await accept(ctx, db, row, actor)
    return await reject(ctx, db, row, actor, reason or "")
