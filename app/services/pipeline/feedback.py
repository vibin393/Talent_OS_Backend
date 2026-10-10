"""Client feedback (story 6.5, FR-38): recorded by a user or given by the contact through the secure links
in the submission email. Both sources go through ``record_feedback``.

* ``interested`` -> ``feedback_status=interested`` (optionally auto-moves to Interviewing, ``system`` actor);
* ``on_hold`` -> status only;
* ``rejected`` -> ``rejected_pending``: a user must confirm (``confirm_rejection``, human-only) before the
  application moves to Rejected with ``client_rejected``. Link-sourced rejections are never auto-applied.
The first feedback stops the demand's ``client_feedback`` SLA clock for that submission.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import check
from app.events.pipeline import ClientFeedbackReceived
from app.models.pipeline import Application, ClientFeedback, PipelineStage, Submission
from app.schemas.pipeline import FeedbackIn, FeedbackOut
from app.services.pipeline.common import (
    FEEDBACK,
    REJECT,
    actor_id,
    actor_kind_of,
    application_for,
    conflict,
    now,
    principal_of,
    settings_row,
    timeline,
)

STATUS_FOR: dict[str, str] = {
    "interested": "interested",
    "on_hold": "on_hold",
    "rejected": "rejected_pending",
}


async def record_feedback(
    ctx: RequestContext,
    db: AsyncSession,
    submission_id: uuid.UUID,
    decision: str,
    comment: str | None,
    *,
    source: str,
    contact_id: uuid.UUID | None = None,
) -> ClientFeedback:
    """Store one feedback (a later one supersedes the earlier; history is kept). 409 on unsent submissions."""
    from app.services import demands as demands_service
    from app.services.pipeline.applications import transition_application

    sub = await db.get(Submission, submission_id, with_for_update=True)
    if sub is None:
        raise NotFound("Submission not found")
    if sub.status != "sent":
        raise conflict(
            "submission_not_sent", "Feedback can only be recorded on a sent submission", status=sub.status
        )
    if decision not in STATUS_FOR:
        raise ValidationFailed("Unknown decision", code="invalid_decision")
    if contact_id is not None and str(contact_id) not in [str(c) for c in sub.recipient_contact_ids or []]:
        raise ValidationFailed("The contact is not a recipient of this submission", code="invalid_contact")
    previous = int(
        (
            await db.execute(
                select(func.count()).select_from(ClientFeedback).where(ClientFeedback.submission_id == sub.id)
            )
        ).scalar_one()
    )
    at = now()
    row = ClientFeedback(
        id=uuid.uuid4(),
        tenant_id=sub.tenant_id,
        submission_id=sub.id,
        application_id=sub.application_id,
        contact_id=contact_id,
        source=source,
        decision=decision,
        comment=(comment or "").strip() or None,
        recorded_by=actor_id(ctx) if source == "recorded" else None,
        received_at=at,
    )
    db.add(row)
    sub.feedback_status = STATUS_FOR[decision]
    app = (
        await db.execute(select(Application).where(Application.id == sub.application_id).with_for_update())
    ).scalar_one()
    app.last_client_feedback = decision
    await db.flush()
    if previous == 0:
        await demands_service.complete_sla_clock(
            ctx, db, sub.demand_id, "client_feedback", ref_id=sub.id, at=at
        )
    data = {
        "submission_id": str(sub.id),
        "application_id": str(app.id),
        "decision": decision,
        "source": source,
    }
    await timeline(
        ctx, db, "candidate", sub.candidate_id, f"{sub.human_id}: client feedback {decision}", data
    )
    await timeline(ctx, db, "demand", sub.demand_id, f"{sub.human_id}: client feedback {decision}", data)
    emit(
        db,
        ClientFeedbackReceived(
            tenant_id=sub.tenant_id,
            feedback_id=row.id,
            submission_id=sub.id,
            application_id=app.id,
            decision=decision,
            source=source,
        ),
    )
    if decision == "interested":
        settings = await settings_row(db)
        stage = await db.get(PipelineStage, app.stage_id)
        if settings.auto_interview_on_interest and stage is not None and stage.key == "submitted":
            await transition_application(ctx, db, app.id, "interviewing", "system", note="Client interested")
    return row


async def _submission_for(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, permission: str
) -> Submission:
    sub = await db.get(Submission, submission_id)
    if sub is None:
        raise NotFound("Submission not found")
    try:
        await application_for(ctx, db, sub.application_id, "application:read")
    except NotFound as exc:
        raise NotFound("Submission not found") from exc
    check(principal_of(ctx), permission)
    return sub


async def record(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, data: FeedbackIn
) -> FeedbackOut:
    await _submission_for(ctx, db, submission_id, FEEDBACK)
    row = await record_feedback(
        ctx, db, submission_id, data.decision, data.comment, source="recorded", contact_id=data.contact_id
    )
    await db.refresh(row)
    return FeedbackOut.model_validate(row)


async def list_feedback(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> list[FeedbackOut]:
    sub = await _submission_for(ctx, db, submission_id, "submission:read")
    rows = (
        await db.execute(
            select(ClientFeedback)
            .where(ClientFeedback.submission_id == sub.id)
            .order_by(ClientFeedback.received_at)
        )
    ).scalars()
    return [FeedbackOut.model_validate(r) for r in rows]


async def _pending(
    ctx: RequestContext, db: AsyncSession, feedback_id: uuid.UUID, permission: str
) -> tuple[ClientFeedback, Submission]:
    row = await db.get(ClientFeedback, feedback_id, with_for_update=True)
    if row is None:
        raise NotFound("Feedback not found")
    try:
        await application_for(ctx, db, row.application_id, "application:read")
    except NotFound as exc:
        raise NotFound("Feedback not found") from exc
    check(principal_of(ctx), permission)
    sub = await db.get(Submission, row.submission_id, with_for_update=True)
    assert sub is not None
    if row.decision != "rejected" or row.confirmed_at is not None or row.dismissed_at is not None:
        raise conflict("feedback_not_pending", "This feedback is not a pending client rejection")
    latest = (
        await db.execute(
            select(ClientFeedback.id)
            .where(ClientFeedback.submission_id == sub.id)
            .order_by(ClientFeedback.received_at.desc(), ClientFeedback.id.desc())
            .limit(1)
        )
    ).scalar_one()
    if latest != row.id or sub.feedback_status != "rejected_pending":
        raise conflict("feedback_superseded", "Newer feedback replaced this rejection")
    return row, sub


async def confirm_rejection(ctx: RequestContext, db: AsyncSession, feedback_id: uuid.UUID) -> FeedbackOut:
    """Human-only: move the application to Rejected with ``client_rejected``."""
    from app.services.pipeline.applications import transition_application

    if actor_kind_of(ctx) != "user":
        raise conflict("human_only_action", "Only a person can confirm a client rejection")
    row, sub = await _pending(ctx, db, feedback_id, REJECT)
    await transition_application(
        ctx, db, row.application_id, "rejected", "user", "client_rejected", row.comment
    )
    row.confirmed_by = actor_id(ctx)
    row.confirmed_at = now()
    sub.feedback_status = "rejected"
    await db.flush()
    await db.refresh(row)
    return FeedbackOut.model_validate(row)


async def dismiss_rejection(ctx: RequestContext, db: AsyncSession, feedback_id: uuid.UUID) -> FeedbackOut:
    """Keep the application where it is (e.g. the client changed their mind by phone)."""
    row, sub = await _pending(ctx, db, feedback_id, FEEDBACK)
    row.dismissed_by = actor_id(ctx)
    row.dismissed_at = now()
    sub.feedback_status = "on_hold"
    await db.flush()
    await db.refresh(row)
    return FeedbackOut.model_validate(row)


async def client_submission_summary(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID
) -> dict[str, Any]:
    """Client 360 ``submissions`` section: counts and the latest sent submissions the caller can see."""
    from app.services.demands import pipeline_port as demands_port
    from app.services.pipeline.common import visible_applications

    principal = principal_of(ctx)
    if "submission:read" not in principal.permissions or "application:read" not in principal.permissions:
        return {"total": 0, "sent": 0, "interested": 0, "rejected": 0, "pending_feedback": 0, "recent": []}
    visible = visible_applications(principal).with_only_columns(Application.id)
    rows = list(
        (
            await db.execute(
                select(Submission)
                .where(Submission.client_id == client_id, Submission.application_id.in_(visible))
                .order_by(Submission.created_at.desc())
            )
        ).scalars()
    )
    sent = [r for r in rows if r.status == "sent"]
    titles = await demands_port.titles(db, list({r.demand_id for r in sent[:10]}))
    return {
        "total": len(rows),
        "sent": len(sent),
        "interested": sum(1 for r in sent if r.feedback_status == "interested"),
        "rejected": sum(1 for r in sent if r.feedback_status in ("rejected", "rejected_pending")),
        "pending_feedback": sum(1 for r in sent if r.feedback_status == "none"),
        "recent": [
            {
                "id": str(r.id),
                "human_id": r.human_id,
                "demand_id": str(r.demand_id),
                "demand_title": (titles.get(r.demand_id) or {}).get("title"),
                "candidate_id": str(r.candidate_id),
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "feedback_status": r.feedback_status,
            }
            for r in sent[:10]
        ],
    }
