"""Submissions to the client (story 6.4, FR-36 / FR-37 / FR-24).

Create validates each shortlisted application (limit, prior submission, consent, recipients, margin); a
below-minimum margin starts the ``margin_override`` approval chain and the email is not sent. Approved
submissions are sent right away by the creating user; after an approval a user clicks Send (FR-65: sending
is human-only). ``send_submission`` snapshots the resume, moves the application to Submitted (``system``
actor), records the candidate/client first, logs the email and emits ``SubmissionSent``; the emails (one
per recipient contact, each with its own three feedback links) go out from a Celery task after commit.
"""

import base64
import re
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.mailer import EmailAttachment, EmailMessage, send_email
from app.core.permissions import COMPENSATION_VIEW, Principal, can_view_compensation, check, mask_compensation
from app.core.sequences import next_human_id
from app.events.pipeline import SubmissionCreated, SubmissionSent
from app.models.pipeline import Application, CandidateClientFirst, ClientFeedback, PipelineStage, Submission
from app.schemas.approvals import SubjectSummary
from app.schemas.pipeline import (
    FeedbackOut,
    MarginOut,
    MarginPreviewIn,
    MoneyOut,
    PerApplicationIn,
    SubmissionCreateItem,
    SubmissionOut,
    SubmissionPreviewIn,
    SubmissionPreviewOut,
    SubmissionsCreate,
    SubmissionsCreateOut,
)
from app.services.pipeline import margin as margin_calc
from app.services.pipeline import resume_pdf, submission_facts  # Epic 61: shared read facts
from app.services.pipeline.common import (
    SUBMISSION_READ,
    SUBMIT,
    acting_as,
    actor_id,
    actor_kind_of,
    application_for,
    conflict,
    demand_for,
    item_savepoint,
    now,
    principal_of,
    settings_row,
    tenant_of,
    timeline,
    user_emails,
)
from app.services.pipeline.settings import burden_for

SUBMISSION_SEQUENCE_KEY = "SUB"
LINK_PURPOSE = "client_feedback"
SUBJECT_TYPE = "submission"
APPROVAL_SUBJECT = "margin_override"
COUNTED_STATUSES = ("pending_approval", "approved", "sent")
DECISIONS = ("interested", "on_hold", "rejected")
_TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_TAG_RE = re.compile(r"<[^>]+>")

# Epic 40 (coordinator decision 10, additive): other domains add attachments to the submission email
# (e.g. assessment result summaries the Recruiter chose to share).
# ``fn(db, submission) -> list[EmailAttachment]``; a provider failure never blocks the email.
AttachmentProvider = Callable[[AsyncSession, Submission], Awaitable[list[EmailAttachment]]]
_attachment_providers: list[AttachmentProvider] = []


def register_submission_attachment_provider(fn: AttachmentProvider) -> None:
    if fn not in _attachment_providers:
        _attachment_providers.append(fn)


async def extra_attachments(db: AsyncSession, sub: Submission) -> list[EmailAttachment]:
    out: list[EmailAttachment] = []
    for fn in _attachment_providers:
        try:
            async with db.begin_nested():
                out.extend(await fn(db, sub))
        except Exception:  # pragma: no cover - never block a submission on an optional attachment
            logger.exception("Submission attachment provider {} failed", getattr(fn, "__name__", fn))
    return out


def _money(amount: Decimal | None, currency: str) -> MoneyOut | None:
    return None if amount is None else MoneyOut(amount=f"{Decimal(amount):.2f}", currency=currency)


def _plain(text: str | None) -> str | None:
    """User-supplied HTML becomes plain text (the template autoescapes it)."""
    if not text:
        return None
    return _TAG_RE.sub("", text).strip() or None


# ================================================================ margin
async def _margin_for(
    db: AsyncSession,
    *,
    employment_type: str | None,
    rate_unit: str | None,
    demand_currency: str | None,
    pay: Any,
    bill: Any,
    pay_unit: str | None,
    bill_unit: str | None,
) -> margin_calc.Margin:
    settings = await settings_row(db)
    return margin_calc.compute(
        pay=pay.amount,
        bill=bill.amount,
        pay_currency=pay.currency,
        bill_currency=bill.currency,
        rate_unit=rate_unit or "hour",
        burden_pct=burden_for(settings, employment_type),
        min_pct=Decimal(settings.min_margin_pct),
        pay_unit=pay_unit,
        bill_unit=bill_unit,
        demand_currency=demand_currency,
    )


def margin_out(m: margin_calc.Margin) -> MarginOut:
    return MarginOut(
        pay_rate=MoneyOut(amount=f"{m.pay:.2f}", currency=m.currency),
        bill_rate=MoneyOut(amount=f"{m.bill:.2f}", currency=m.currency),
        rate_unit=m.rate_unit,
        burden_pct=f"{m.burden_pct:.2f}",
        margin_amount=MoneyOut(amount=f"{m.amount:.2f}", currency=m.currency),
        margin_pct=f"{m.pct:.2f}",
        min_margin_pct=f"{m.min_pct:.2f}",
        below_min_margin=m.below_min,
    )


async def margin_preview(ctx: RequestContext, db: AsyncSession, data: MarginPreviewIn) -> MarginOut:
    """Live margin for the wizard; requires ``compensation:view`` (403)."""
    check(principal_of(ctx), COMPENSATION_VIEW)
    employment_type, rate_unit, currency = data.employment_type, data.rate_unit, None
    if data.demand_id is not None:
        demand = await demand_for(ctx, db, data.demand_id, SUBMIT)
        employment_type = demand.employment_type or employment_type
        rate_unit = demand.rate_unit or rate_unit  # type: ignore[assignment]
        currency = demand.currency
    m = await _margin_for(
        db,
        employment_type=employment_type,
        rate_unit=rate_unit,
        demand_currency=currency,
        pay=data.pay_rate,
        bill=data.bill_rate,
        pay_unit=data.pay_rate_unit,
        bill_unit=data.bill_rate_unit,
    )
    return margin_out(m)


# ================================================================ read models
def _uuid_list(values: list[Any]) -> list[uuid.UUID]:
    return [uuid.UUID(str(v)) for v in values or []]


async def submission_out(ctx: RequestContext, db: AsyncSession, sub: Submission) -> dict[str, Any]:
    feedback = (
        await db.execute(
            select(ClientFeedback)
            .where(ClientFeedback.submission_id == sub.id)
            .order_by(ClientFeedback.received_at)
        )
    ).scalars()
    out = SubmissionOut(
        id=sub.id,
        human_id=sub.human_id,
        application_id=sub.application_id,
        demand_id=sub.demand_id,
        client_id=sub.client_id,
        candidate_id=sub.candidate_id,
        status=sub.status,
        pay_rate=_money(sub.pay_rate, sub.currency),
        bill_rate=_money(sub.bill_rate, sub.currency),
        rate_unit=sub.rate_unit,
        employment_type=sub.employment_type,
        burden_pct=f"{Decimal(sub.burden_pct):.2f}",
        margin_amount=_money(sub.margin_amount, sub.currency),
        margin_pct=f"{Decimal(sub.margin_pct):.2f}",
        below_min_margin=sub.below_min_margin,
        approval_request_id=sub.approval_request_id,
        resume_kind=sub.resume_kind,
        resume_file_id=sub.resume_file_id,
        resume_sha256=sub.resume_sha256,
        resume_download_url=f"/api/v1/files/{sub.resume_file_id}/download" if sub.resume_file_id else None,
        note=sub.note,
        recipient_contact_ids=_uuid_list(sub.recipient_contact_ids),
        cc_user_ids=_uuid_list(sub.cc_user_ids),
        email_subject=sub.email_subject,
        prior_submission_reason=sub.prior_submission_reason,
        consent_id=sub.consent_id,
        created_by=sub.created_by,
        created_at=sub.created_at,
        sent_at=sub.sent_at,
        sent_by=sub.sent_by,
        cancelled_at=sub.cancelled_at,
        feedback_status=sub.feedback_status,
        feedback=[FeedbackOut.model_validate(f) for f in feedback],
    )
    principal = ctx.principal if isinstance(ctx.principal, Principal) else None
    return dict(mask_compensation(out, principal))


async def _submission_for(
    ctx: RequestContext,
    db: AsyncSession,
    submission_id: uuid.UUID,
    permission: str,
    *,
    for_update: bool = False,
) -> tuple[Submission, Application]:
    sub = await db.get(Submission, submission_id, with_for_update=for_update)
    if sub is None:
        raise NotFound("Submission not found")
    try:
        app = await application_for(ctx, db, sub.application_id, "application:read")
    except NotFound as exc:
        raise NotFound("Submission not found") from exc
    principal = principal_of(ctx)
    if principal.type == "user":
        check(principal, permission)
    return sub, app


async def get_submission(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> dict[str, Any]:
    sub, _ = await _submission_for(ctx, db, submission_id, SUBMISSION_READ)
    return await submission_out(ctx, db, sub)


async def list_for_demand(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
) -> list[dict[str, Any]]:
    demand = await demand_for(ctx, db, demand_id, SUBMISSION_READ)
    rows = (
        await db.execute(
            select(Submission).where(Submission.demand_id == demand.id).order_by(Submission.created_at.desc())
        )
    ).scalars()
    return [await submission_out(ctx, db, s) for s in rows]


async def submission_access(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, write: bool
) -> bool:
    """File access hook: the submission's resume snapshot follows the application's visibility."""
    try:
        await _submission_for(ctx, db, submission_id, SUBMIT if write else SUBMISSION_READ)
    except DomainError:
        return False
    return True


async def resume_file(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> uuid.UUID:
    sub, _ = await _submission_for(ctx, db, submission_id, SUBMISSION_READ)
    if sub.resume_file_id is None:
        raise NotFound("No resume snapshot for this submission yet")
    return sub.resume_file_id


# ================================================================ create
async def _stage_key(db: AsyncSession, app: Application) -> str:
    stage = await db.get(PipelineStage, app.stage_id)
    return stage.key if stage is not None else ""


async def _counted(db: AsyncSession, demand_id: uuid.UUID) -> int:
    return await submission_facts.counted_submissions(db, demand_id)


async def _prior(
    db: AsyncSession, candidate_id: uuid.UUID, client_id: uuid.UUID, window_days: int
) -> Submission | None:
    return await submission_facts.prior_submission(db, candidate_id, client_id, window_days)


async def _consent(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    client_id: uuid.UUID,
    per: PerApplicationIn,
    mode: str,
) -> uuid.UUID | None:
    from app.models.privacy import Consent
    from app.services import privacy

    if await privacy.has_active_consent(db, candidate_id, "client_submission", client_id):
        row = (
            await db.execute(
                select(Consent.id)
                .where(
                    Consent.candidate_id == candidate_id,
                    Consent.purpose == "client_submission",
                    Consent.client_id == client_id,
                    Consent.status == "granted",
                )
                .order_by(Consent.granted_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row
    attestation = per.consent_attestation
    if mode == "attestation_allowed" and attestation is not None and attestation.confirmed:
        consent = await privacy.grant_consent(
            ctx,
            db,
            candidate_id,
            "client_submission",
            source="recruiter_attestation",
            client_id=client_id,
            evidence={"attestation_text": attestation.text, "attested_by": str(actor_id(ctx))},
        )
        return consent.id
    raise conflict(
        "submission_consent_required",
        "The candidate has not consented to be submitted to this client",
        candidate_id=str(candidate_id),
        client_id=str(client_id),
        attestation_allowed=mode == "attestation_allowed",
    )


async def _check_recipients(
    db: AsyncSession, client_id: uuid.UUID, contact_ids: list[uuid.UUID], cc_user_ids: list[uuid.UUID]
) -> None:
    from app.services import clients_service

    contacts = await clients_service.contacts_of_client(db, client_id, contact_ids)
    bad = [
        str(c)
        for c in contact_ids
        if c not in contacts or not contacts[c]["active"] or not contacts[c]["email"]
    ]
    if bad:
        raise ValidationFailed(
            "Recipients must be active contacts of the demand's client with an email address",
            code="invalid_recipients",
            details={"contact_ids": bad},
        )
    if cc_user_ids:
        emails = await user_emails(db, cc_user_ids)
        missing = [str(u) for u in cc_user_ids if u not in emails]
        if missing:
            raise ValidationFailed("Unknown CC users", code="invalid_cc_users", details={"user_ids": missing})


async def _check_resume(db: AsyncSession, candidate_id: uuid.UUID, kind: str) -> None:
    from app.services.candidates import pipeline_port as candidates

    if kind == "client_formatted":
        if await candidates.get_resume_profile(db, candidate_id) is None:
            raise conflict(
                "resume_not_parsed", "The candidate's resume is not parsed; send the original instead"
            )
    elif await candidates.get_primary_resume_file(db, candidate_id) is None:
        raise conflict("resume_missing", "The candidate has no resume on file")


async def _create_one(
    ctx: RequestContext,
    db: AsyncSession,
    demand: Any,
    app_id: uuid.UUID,
    data: SubmissionsCreate,
    created_so_far: int,
    mode: str,
    window_days: int,
) -> Submission:
    app = await application_for(ctx, db, app_id, SUBMIT, for_update=True)
    if app.demand_id != demand.id:
        raise NotFound("Application not found")
    per = data.per_application.get(app_id)
    if per is None:
        raise ValidationFailed("Rates are required for every application", code="rates_required")
    if await _stage_key(db, app) != "shortlisted":
        raise conflict("not_shortlisted", "Only shortlisted applications can be submitted")
    if demand.submission_limit is not None:
        if await _counted(db, demand.id) + created_so_far + 1 > demand.submission_limit:
            raise conflict(
                "submission_limit_reached",
                "The demand's submission limit is reached",
                limit=demand.submission_limit,
            )
    prior = await _prior(db, app.candidate_id, demand.client_id, window_days)
    reason = (per.prior_submission_reason or "").strip() or None
    if prior is not None:
        if reason is None:
            raise conflict(
                "prior_submission_warning",
                "This candidate was submitted to this client recently; give a reason to continue",
                last_submitted_at=prior.sent_at.isoformat() if prior.sent_at else None,
                demand_id=str(prior.demand_id),
                by=str(prior.sent_by) if prior.sent_by else None,
            )
        if len(reason) < 10:
            raise ValidationFailed(
                "The reason must be at least 10 characters",
                code="validation_failed",
                details={"fields": {"prior_submission_reason": "At least 10 characters"}},
            )
    else:
        reason = None
    m = await _margin_for(
        db,
        employment_type=demand.employment_type,
        rate_unit=demand.rate_unit,
        demand_currency=demand.currency,
        pay=per.pay_rate,
        bill=per.bill_rate,
        pay_unit=per.pay_rate_unit,
        bill_unit=per.bill_rate_unit,
    )
    # Epic 16 (FR-91): the submission bill rate must clear the client's rate card (409 rate_exception_*).
    from app.services import rate_gate_port

    await rate_gate_port.check(
        "submission_create",
        ctx,
        db,
        demand=demand,
        application_id=app.id,
        candidate_id=app.candidate_id,
        bill_rate=m.bill,
        currency=m.currency,
        rate_unit=m.rate_unit,
    )
    await _check_resume(db, app.candidate_id, per.resume_kind)
    consent_id = await _consent(ctx, db, app.candidate_id, demand.client_id, per, mode)
    from app.services import settings_service

    prefix = await settings_service.id_prefix(db, SUBMISSION_SEQUENCE_KEY)
    sub = Submission(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, SUBMISSION_SEQUENCE_KEY, prefix, width=6),
        application_id=app.id,
        demand_id=demand.id,
        client_id=demand.client_id,
        candidate_id=app.candidate_id,
        status="pending_approval" if m.below_min else "approved",
        pay_rate=m.pay,
        bill_rate=m.bill,
        currency=m.currency,
        rate_unit=m.rate_unit,
        employment_type=demand.employment_type,
        burden_pct=m.burden_pct,
        margin_amount=m.amount,
        margin_pct=m.pct,
        below_min_margin=m.below_min,
        resume_kind=per.resume_kind,
        note=data.note,
        recipient_contact_ids=[str(c) for c in dict.fromkeys(data.recipient_contact_ids)],
        cc_user_ids=[str(u) for u in dict.fromkeys(data.cc_user_ids)],
        email_subject=(data.subject or "").strip() or None,
        email_body=_plain(data.body_html),
        prior_submission_reason=reason,
        consent_id=consent_id,
        created_by=actor_id(ctx),
        feedback_status="none",
    )
    db.add(sub)
    await db.flush()
    if reason is not None:
        audit.record(
            "submission.prior_submission_override",
            "submission",
            sub.id,
            session=db,
            prior_submission_id=str(prior.id) if prior else None,
            reason=reason,
        )
    if m.below_min:
        from app.services import approvals

        result = await approvals.start(
            ctx,
            db,
            APPROVAL_SUBJECT,
            sub.id,
            {
                "submission": sub.human_id,
                "demand_id": str(demand.id),
                "client_id": str(demand.client_id),
                "margin_pct": str(m.pct),
                "min_margin_pct": str(m.min_pct),
                "bill_rate": str(m.bill),
                "currency": m.currency,
                "rate_unit": m.rate_unit,
            },
        )
        sub.approval_request_id = result.request_id
        if result.status == "auto_approved":
            sub.status = "approved"
        await db.flush()
    emit(
        db,
        SubmissionCreated(
            tenant_id=sub.tenant_id,
            submission_id=sub.id,
            application_id=app.id,
            demand_id=demand.id,
            client_id=demand.client_id,
            status=sub.status,
            below_min_margin=m.below_min,
            origin_chain_depth=int(getattr(ctx, "workflow_chain_depth", 0) or 0),
        ),
    )
    return sub


async def create_submissions(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: SubmissionsCreate
) -> SubmissionsCreateOut:
    """Per application: ``sent`` / ``pending_approval`` / ``blocked`` (with the error). A request for a single
    application raises its error instead (409 / 422)."""
    demand = await demand_for(ctx, db, demand_id, SUBMIT)
    if actor_kind_of(ctx) != "user":
        raise conflict("human_only_action", "Only a person can submit candidates to a client")
    settings = await settings_row(db)
    await _check_recipients(db, demand.client_id, list(data.recipient_contact_ids), list(data.cc_user_ids))
    ids = list(dict.fromkeys(data.application_ids))
    items: list[SubmissionCreateItem] = []
    created = 0
    for app_id in ids:
        try:
            async with item_savepoint(db):
                sub = await _create_one(
                    ctx,
                    db,
                    demand,
                    app_id,
                    data,
                    created,
                    settings.submission_consent_mode,
                    settings.prior_submission_window_days,
                )
                if sub.status == "approved":
                    await send_submission(ctx, db, sub.id, "user")
            created += 1
            items.append(
                SubmissionCreateItem(
                    application_id=app_id,
                    status="sent" if sub.status == "sent" else "pending_approval",
                    submission_id=sub.id,
                )
            )
        except DomainError as exc:
            if len(ids) == 1:
                raise
            items.append(
                SubmissionCreateItem(
                    application_id=app_id,
                    status="blocked",
                    error={
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                        "status": exc.status_code,
                    },
                )
            )
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return SubmissionsCreateOut(items=items, counts=counts)


# ================================================================ send (human-only, FR-65)
async def _snapshot_resume(ctx: RequestContext, db: AsyncSession, sub: Submission) -> None:
    """Copy the chosen resume into storage as the submission's own file (later edits never change it)."""
    from app.services import files_service, identity_bridge
    from app.services.candidates import pipeline_port as candidates

    if sub.resume_file_id is not None:
        return
    if sub.resume_kind == "client_formatted":
        profile = await candidates.get_resume_profile(db, sub.candidate_id)
        if profile is None:
            raise conflict(
                "resume_not_parsed", "The candidate's resume is not parsed; send the original instead"
            )
        tenant = await identity_bridge.get_tenant(db, sub.tenant_id)
        data = resume_pdf.render(
            profile, tenant_name=tenant.name if tenant else "Talent OS", submission_ref=sub.human_id
        )
        filename = f"{sub.human_id}-profile.pdf"
    else:
        file_id = await candidates.get_primary_resume_file(db, sub.candidate_id)
        original = await files_service.get_file(db, file_id) if file_id else None
        if original is None:
            raise conflict("resume_missing", "The candidate has no resume on file")
        data = await files_service.read_file(original)
        ext = "pdf" if original.content_type == "application/pdf" else "docx"
        filename = f"{sub.human_id}-resume.{ext}"
    stored = await files_service.store_upload(ctx, db, data, filename, {"pdf", "docx"}, SUBJECT_TYPE, sub.id)
    sub.resume_file_id = stored.id
    sub.resume_sha256 = stored.sha256


async def send_submission(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, actor: str | None = None
) -> Submission:
    """Human-only (FR-65): ``ai`` / ``workflow`` / ``system`` actors get 409 ``human_only_action``."""
    from app.services import demands as demands_service
    from app.services.pipeline.applications import _sync_demand_counts, transition_application

    kind = actor or actor_kind_of(ctx)
    if kind != "user":
        raise conflict("human_only_action", "Only a person can send a submission")
    sub = await db.get(Submission, submission_id, with_for_update=True)
    if sub is None:
        raise NotFound("Submission not found")
    if sub.status == "sent":
        raise conflict("already_sent", "This submission was already sent")
    if sub.status != "approved":
        raise conflict(
            "submission_not_approved", "The submission is not approved for sending", status=sub.status
        )
    app = (
        await db.execute(select(Application).where(Application.id == sub.application_id).with_for_update())
    ).scalar_one()
    if await _stage_key(db, app) != "shortlisted":
        raise conflict("not_shortlisted", "Only shortlisted applications can be submitted")
    await _snapshot_resume(ctx, db, sub)
    if not sub.email_subject:
        from app.services.demands import pipeline_port as demands_port

        demand = await demands_port.get_row(db, sub.demand_id)
        sub.email_subject = f"Candidate submission for {demand.title} ({sub.human_id})"
    at = now()
    sub.status = "sent"
    sub.sent_at = at
    sub.sent_by = actor_id(ctx)
    await db.flush()
    await transition_application(ctx, db, app.id, "submitted", "system")
    with acting_as(db, "system"):
        try:
            async with db.begin_nested():
                db.add(
                    CandidateClientFirst(
                        id=uuid.uuid4(),
                        tenant_id=sub.tenant_id,
                        client_id=sub.client_id,
                        candidate_id=sub.candidate_id,
                        submission_id=sub.id,
                        first_submitted_by=sub.sent_by,
                        first_submitted_at=at,
                    )
                )
                await db.flush()
        except IntegrityError:
            pass  # not the first submission of this candidate to this client (FR-24 keeps the first)
    body = f"{sub.human_id}: submitted to client ({app.human_id})"
    data = {
        "submission_id": str(sub.id),
        "application_id": str(app.id),
        "recipients": len(sub.recipient_contact_ids),
    }
    for entity_type, entity_id in (
        ("candidate", sub.candidate_id),
        ("demand", sub.demand_id),
        ("client", sub.client_id),
    ):
        await timeline(ctx, db, entity_type, entity_id, body, data, kind="email")
    if sub.sent_by is not None:
        await demands_service.record_first_submission(ctx, db, sub.demand_id, sub.sent_by, at)
    await demands_service.start_sla_clock(ctx, db, sub.demand_id, "client_feedback", ref_id=sub.id, at=at)
    await _sync_demand_counts(ctx, db, sub.demand_id)
    emit(
        db,
        SubmissionSent(
            tenant_id=sub.tenant_id,
            submission_id=sub.id,
            application_id=app.id,
            demand_id=sub.demand_id,
            client_id=sub.client_id,
        ),
    )
    return sub


async def send(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> dict[str, Any]:
    await _submission_for(ctx, db, submission_id, SUBMIT)
    sub = await send_submission(ctx, db, submission_id)
    await db.refresh(sub)
    return await submission_out(ctx, db, sub)


async def cancel(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> dict[str, Any]:
    from app.services import approvals

    sub, _ = await _submission_for(ctx, db, submission_id, SUBMIT, for_update=True)
    if sub.status in ("sent", "cancelled"):
        raise conflict(
            "submission_not_cancellable", "Only unsent submissions can be cancelled", status=sub.status
        )
    if sub.status == "pending_approval":
        await approvals.cancel_open(ctx, db, APPROVAL_SUBJECT, sub.id)
    sub.status = "cancelled"
    sub.cancelled_at = now()
    await db.flush()
    await db.refresh(sub)
    return await submission_out(ctx, db, sub)


# ================================================================ approvals callbacks
async def _on_approved(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    sub = await db.get(Submission, submission_id, with_for_update=True)
    if sub is None or sub.status != "pending_approval":
        return
    sub.status = "approved"
    await db.flush()


async def _on_returned(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    submission_id: uuid.UUID,
    outcome: str,  # ruff: ignore[unused-function-argument]
    comment: str,  # ruff: ignore[unused-function-argument]
) -> None:
    sub = await db.get(Submission, submission_id, with_for_update=True)
    if sub is None or sub.status != "pending_approval":
        return
    sub.status = "approval_rejected"
    await db.flush()


async def _describe(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, SubjectSummary]:
    from app.services.demands import pipeline_port as demands_port

    rows = list((await db.execute(select(Submission).where(Submission.id.in_(ids)))).scalars())
    titles = await demands_port.titles(db, list({r.demand_id for r in rows}))
    return {
        r.id: SubjectSummary(label=r.human_id, title=(titles.get(r.demand_id) or {}).get("title"))
        for r in rows
    }


def register_approvals() -> None:
    from app.services import approvals

    approvals.register_subject(APPROVAL_SUBJECT, _on_approved, _on_returned, _describe)


# ================================================================ email rendering / delivery
def _experience(months: int | None) -> str | None:
    if not months:
        return None
    years = months / 12
    return f"{years:.1f} years"


async def _email_context(db: AsyncSession, sub: Submission, *, include_bill_rate: bool) -> dict[str, Any]:
    from app.services import identity_bridge
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    tenant = await identity_bridge.get_tenant(db, sub.tenant_id)
    demand = await demands_port.get_row(db, sub.demand_id)
    cand = (await candidates.summaries(db, [sub.candidate_id])).get(sub.candidate_id, {})
    notice = cand.get("notice_period_days")
    loc = cand.get("location") or {}
    return {
        "tenant_name": tenant.name if tenant else "Talent OS",
        "demand_title": demand.title,
        "demand_ref": demand.req_id,
        "submission_ref": sub.human_id,
        "candidate": {
            "full_name": cand.get("full_name"),
            "current_title": cand.get("current_title"),
            "experience": _experience(cand.get("total_experience_months")),
            "notice": f"{notice} days" if notice is not None else None,
            "location": ", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x)
            or None,
        },
        "bill_rate": f"{sub.currency} {Decimal(sub.bill_rate):,.2f} / {sub.rate_unit}"
        if include_bill_rate
        else None,
        "note": sub.note,
        "body_html": sub.email_body,
    }


def render_email(
    context: dict[str, Any],
    *,
    subject: str,
    contact_name: str | None,
    links: dict[str, str] | None,
    attachment_name: str | None,
) -> str:
    return _TEMPLATES.get_template("submission_email.html.j2").render(
        **context, subject=subject, contact_name=contact_name, links=links, attachment_name=attachment_name
    )


def _text_version(context: dict[str, Any], links: dict[str, str] | None) -> str:
    c = context["candidate"]
    lines = [
        f"Candidate for {context['demand_title']} ({context['submission_ref']})",
        f"Candidate: {c.get('full_name') or ''}",
    ]
    for label, key in (
        ("Current title", "current_title"),
        ("Experience", "experience"),
        ("Notice period", "notice"),
        ("Location", "location"),
    ):
        if c.get(key):
            lines.append(f"{label}: {c[key]}")
    if context.get("bill_rate"):
        lines.append(f"Bill rate: {context['bill_rate']}")
    if context.get("note"):
        lines += ["", context["note"]]
    if links:
        lines += [
            "",
            f"Interested: {links['interested']}",
            f"On hold: {links['on_hold']}",
            f"Not a fit: {links['rejected']}",
        ]
    lines += ["", f"Submitted by {context['tenant_name']}"]
    return "\n".join(lines)


async def deliver_emails(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> int:
    """Task body: one email per recipient contact with its own three feedback links (idempotent per contact
    via ``email_message_ids``). Pay rate and margin are never included."""
    from app.services import clients_service, files_service, settings_service

    sub = await db.get(Submission, submission_id, with_for_update=True)
    if sub is None or sub.status != "sent":
        return 0
    settings = await settings_row(db)
    context = await _email_context(db, sub, include_bill_rate=settings.show_bill_rate_in_submission)
    contacts = await clients_service.contacts_of_client(
        db, sub.client_id, _uuid_list(sub.recipient_contact_ids)
    )
    cc = tuple((await user_emails(db, _uuid_list(sub.cc_user_ids))).values())
    reply_to = (await user_emails(db, [sub.sent_by])).get(sub.sent_by) if sub.sent_by else None
    attachment: EmailAttachment | None = None
    if sub.resume_file_id is not None:
        stored = await files_service.get_file(db, sub.resume_file_id)
        if stored is not None:
            attachment = EmailAttachment(
                filename=stored.original_name,
                content_type=stored.content_type,
                data=await files_service.read_file(stored),
            )
    extras = await extra_attachments(db, sub)  # Epic 40: shared assessment results (chosen per submission)
    ttl = await settings_service.link_ttl(db, LINK_PURPOSE)
    sent_ids = dict(sub.email_message_ids or {})
    delivered = 0
    for contact_id, info in contacts.items():
        if str(contact_id) in sent_ids or not info.get("email"):
            continue
        for link in await secure_links.links_for_subject(db, SUBJECT_TYPE, sub.id):
            if (link.meta or {}).get("contact_id") == str(contact_id) and link.consumed_at is None:
                await secure_links.revoke(db, link.id)
        links: dict[str, str] = {}
        for decision in DECISIONS:
            _, token = await secure_links.issue(
                db,
                tenant_id=sub.tenant_id,
                purpose=LINK_PURPOSE,
                subject_type=SUBJECT_TYPE,
                subject_id=sub.id,
                ttl=ttl,
                metadata={"contact_id": str(contact_id), "decision": decision},
                created_by=sub.sent_by,
            )
            links[decision] = secure_links.link_url(token)
        subject = sub.email_subject or f"Candidate submission ({sub.human_id})"
        message = EmailMessage(
            to=str(info["email"]),
            subject=subject,
            text=_text_version(context, links),
            html=render_email(
                context,
                subject=subject,
                contact_name=info.get("name"),
                links=links,
                attachment_name=attachment.filename if attachment else None,
            ),
            tenant_id=sub.tenant_id,
            kind="submission",
            cc=cc,
            reply_to=reply_to,
            attachments=((attachment,) if attachment else ()) + tuple(extras),
        )
        await db.flush()
        await send_email(message)
        sent_ids[str(contact_id)] = str(message.id)
        sub.email_message_ids = dict(sent_ids)
        await db.flush()
        delivered += 1
    del ctx
    logger.info("Submission {} emails delivered: {}", sub.id, delivered)
    return delivered


async def preview(ctx: RequestContext, db: AsyncSession, data: SubmissionPreviewIn) -> SubmissionPreviewOut:
    """Render the email (placeholder links) and resume preview; persists nothing."""
    from app.services import clients_service, identity_bridge
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    app = await application_for(ctx, db, data.application_id, SUBMIT)
    demand = await demands_port.get_row(db, app.demand_id)
    settings = await settings_row(db)
    draft = Submission(
        id=uuid.uuid4(),
        tenant_id=app.tenant_id,
        human_id="SUB-PREVIEW",
        application_id=app.id,
        demand_id=demand.id,
        client_id=demand.client_id,
        candidate_id=app.candidate_id,
        currency=data.bill_rate.currency if data.bill_rate else (demand.currency or "INR"),
        bill_rate=data.bill_rate.amount if data.bill_rate else Decimal(0),
        rate_unit=demand.rate_unit or "hour",
        note=data.note,
        email_body=_plain(data.body_html),
    )
    context = await _email_context(
        db, draft, include_bill_rate=settings.show_bill_rate_in_submission and data.bill_rate is not None
    )
    contacts = await clients_service.contacts_of_client(
        db, demand.client_id, list(data.recipient_contact_ids)
    )
    first = next(iter(contacts.values()), {})
    subject = (data.subject or "").strip() or f"Candidate submission for {demand.title}"
    placeholder = dict.fromkeys(DECISIONS, "#")
    html = render_email(
        context, subject=subject, contact_name=first.get("name"), links=placeholder, attachment_name=None
    )
    resume_b64: str | None = None
    error: str | None = None
    available = True
    if data.resume_kind == "client_formatted":
        profile = await candidates.get_resume_profile(db, app.candidate_id)
        if profile is None:
            available, error = False, "resume_not_parsed"
        else:
            tenant = await identity_bridge.get_tenant(db, app.tenant_id)
            pdf = resume_pdf.render(
                profile, tenant_name=tenant.name if tenant else "Talent OS", submission_ref="PREVIEW"
            )
            resume_b64 = base64.b64encode(pdf).decode("ascii")
    elif await candidates.get_primary_resume_file(db, app.candidate_id) is None:
        available, error = False, "resume_missing"
    del ctx
    return SubmissionPreviewOut(
        subject=subject,
        html=html,
        resume_kind=data.resume_kind,
        resume_available=available,
        resume_error=error,
        resume_pdf_base64=resume_b64,
    )


def can_view_margin(ctx: RequestContext) -> bool:
    principal = ctx.principal if isinstance(ctx.principal, Principal) else None
    return can_view_compensation(principal)
