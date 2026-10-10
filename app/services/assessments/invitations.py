# ruff: noqa: E501, ARG001
"""Invitations (Epic 40 A5, FR-203): invite (the AD-39 stub), bulk, extend, cancel, the star (human-only) decision,
reminders, expiry and the internal read models (Candidate 360 results, pipeline badges).

* ``invite`` -- one Candidate per call; actor ``user`` or ``workflow`` (section 5.1); requires the ``assessment``
  consent notice (409 ``assessment_notice_missing``) and a published Assessment (409 ``assessment_not_published``);
  idempotent on ``idempotency_key``; pinned to ``current_version_id``; ``gating=None`` takes ``gating_default``; a
  second open invitation -> 409 ``invitation_exists``. Built-in delivery issues a multi-use Secure Link (purpose
  ``assessment_attempt``, TTL = deadline + ``link_grace_days``) and emails it (the token is never logged or stored);
  provider delivery queues a ``connectors`` job (``provider.py``).
* ``decide`` never moves the pipeline (the Recruiter does that separately).
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.events import emit
from app.models.assessments import (
    INVITATION_OPEN_STATUSES,
    Assessment,
    AssessmentInvitation,
    Attempt,
    AttemptAnswer,
    ProctoringSignal,
)
from app.schemas.assessments import (
    AssessmentInvitationSummary,
    AssessmentInviteIn,
    BadgeOut,
    BadgesOut,
    BulkInviteIn,
    BulkInviteOut,
    BulkResult,
    CancelIn,
    DecideIn,
    ExtendIn,
    InvitationOut,
    InvitationPatch,
)
from app.services.assessments import lifecycle
from app.services.assessments import settings as settings_mod
from app.services.assessments.common import (
    CONSENT_PURPOSE,
    LINK_PURPOSE,
    LINK_SUBJECT,
    actor_of,
    conflict,
    invalid,
    not_found,
    now,
    tenant_of,
    user_id,
)
from app.services.assessments.events import AssessmentInvitationStatusChanged, AssessmentReminderDue

LINK_MAX_USES = 100_000  # multi-use for resume; the attempt session (X-Attempt-Session) guards concurrency


def summary(inv: AssessmentInvitation) -> AssessmentInvitationSummary:
    return AssessmentInvitationSummary(
        id=inv.id,
        assessment_id=inv.assessment_id,
        candidate_id=inv.candidate_id,  # type: ignore[arg-type]
        application_id=inv.application_id,
        context_kind=inv.context_kind,
        context_id=inv.context_id,
        status=inv.status,
        gating=inv.gating,
        deadline_at=inv.deadline_at,
        result=inv.result,
        score=inv.score,
        scored_at=inv.scored_at,
    )


async def load(
    db: AsyncSession, invitation_id: uuid.UUID, *, for_update: bool = False
) -> AssessmentInvitation:
    stmt = select(AssessmentInvitation).where(
        AssessmentInvitation.id == invitation_id, AssessmentInvitation.candidate_id.is_not(None)
    )
    if for_update:
        stmt = stmt.with_for_update()
    inv = (await db.execute(stmt)).scalar_one_or_none()
    if inv is None:
        raise not_found("Invitation")
    return inv


# ---------------------------------------------------------------- candidate facts (P1 tables, read only)
async def candidate_names(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.candidates import Candidate

    if not ids:
        return {}
    rows = await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(set(ids))))
    return dict(rows.tuples().all())


async def candidate_email(db: AsyncSession, candidate_id: uuid.UUID) -> str | None:
    from app.models.candidates import CandidateEmail

    row = (
        await db.execute(
            select(CandidateEmail.email)
            .where(CandidateEmail.candidate_id == candidate_id)
            .order_by(CandidateEmail.is_primary.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(row) if row else None


async def _candidate_ok(db: AsyncSession, candidate_id: uuid.UUID) -> None:
    from app.models.candidates import Candidate

    cand = await db.get(Candidate, candidate_id)
    if (
        cand is None
        or getattr(cand, "erased_at", None) is not None
        or getattr(cand, "merged_into_id", None) is not None
    ):
        raise not_found("Candidate")


async def _application_ok(db: AsyncSession, application_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
    from app.models.pipeline import Application

    app = await db.get(Application, application_id)
    if app is None or app.candidate_id != candidate_id:
        raise invalid(
            "application_mismatch", "The application does not belong to this candidate", "application_id"
        )


# ---------------------------------------------------------------- emails
async def _email(inv: AssessmentInvitation, to: str, subject: str, body: str, kind: str) -> None:
    from app.core.mailer import EmailMessage, send_email

    await send_email(EmailMessage(to=to, subject=subject, text=body, tenant_id=inv.tenant_id, kind=kind))


async def issue_link(
    db: AsyncSession, inv: AssessmentInvitation, grace_days: int, created_by: uuid.UUID | None
) -> str:
    """Issue (or re-issue) the multi-use attempt link; returns the raw token (never stored or logged)."""
    if inv.secure_link_id is not None:
        await secure_links.revoke(db, inv.secure_link_id)
    ttl = max(inv.deadline_at - now(), timedelta(minutes=5)) + timedelta(days=grace_days)
    link, token = await secure_links.issue(
        db,
        tenant_id=inv.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type=LINK_SUBJECT,
        subject_id=inv.id,
        ttl=ttl,
        max_uses=LINK_MAX_USES,
        metadata={"audience": "candidate"},
        created_by=created_by,
    )
    inv.secure_link_id = link.id
    return token


async def send_invite_email(
    db: AsyncSession, inv: AssessmentInvitation, name: str, token: str, *, extended: bool = False
) -> bool:
    email = await candidate_email(db, inv.candidate_id) if inv.candidate_id else None
    if not email:
        return False
    when = inv.deadline_at.strftime("%d %b %Y %H:%M UTC")
    lead = (
        "Your assessment deadline was extended."
        if extended
        else "You are invited to take an online assessment."
    )
    await _email(
        inv,
        email,
        f"Assessment: {name}",
        f"{lead}\n\nAssessment: {name}\nComplete it by: {when}\n\nStart here (you can resume from the same link): "
        f"{secure_links.link_url(token)}\n\nThe test is timed by our server once you start. We log tab switches, focus loss, "
        "copy / paste and full-screen exits during the test; they never change your score.",
        "assessment_invited",
    )
    return True


# ---------------------------------------------------------------- invite (AD-39 contract)
def _emit_created(db: AsyncSession, inv: AssessmentInvitation, actor_kind: str) -> None:
    emit(
        db,
        AssessmentInvitationStatusChanged(
            tenant_id=inv.tenant_id,
            invitation_id=inv.id,
            assessment_id=inv.assessment_id,
            candidate_id=inv.candidate_id,
            application_id=inv.application_id,
            from_status=None,
            to_status="invited",
            actor_kind=actor_kind,
            inviter_id=inv.invited_by_id if actor_kind == "user" else None,
        ),
    )


async def invite(
    ctx: RequestContext, db: AsyncSession, req: AssessmentInviteIn
) -> AssessmentInvitationSummary:
    from app.services.privacy import consent

    actor = actor_of(ctx)
    lifecycle.require(None, "invited", actor.kind)
    if req.idempotency_key:
        existing = (
            await db.execute(
                select(AssessmentInvitation).where(
                    AssessmentInvitation.idempotency_key == req.idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return summary(existing)
    assessment = await db.get(Assessment, req.assessment_id)
    if assessment is None:
        raise not_found()
    if assessment.status != "published" or assessment.current_version_id is None:
        raise conflict("assessment_not_published", "Only published assessments can be sent")
    tenant_id = tenant_of(db)
    async with db.begin_nested():
        notice = await consent.active_notice(db, tenant_id, CONSENT_PURPOSE)
    if notice is None:
        raise conflict("assessment_notice_missing", "The assessment privacy notice is not available")
    await _candidate_ok(db, req.candidate_id)
    if req.context_kind == "application" and req.application_id is None:
        raise invalid("application_required", "Pipeline invitations need the application", "application_id")
    if req.application_id is not None:
        await _application_ok(db, req.application_id, req.candidate_id)
    s = await settings_mod.current(db)
    deadline: datetime = req.deadline_at or (now() + timedelta(days=s.default_deadline_days))
    if deadline <= now():
        raise invalid("deadline_in_past", "The deadline must be in the future", "deadline_at")
    open_row = (
        await db.execute(
            select(AssessmentInvitation.id).where(
                AssessmentInvitation.assessment_id == assessment.id,
                AssessmentInvitation.candidate_id == req.candidate_id,
                AssessmentInvitation.status.in_(INVITATION_OPEN_STATUSES),
            )
        )
    ).first()
    if open_row is not None:
        raise conflict(
            "invitation_exists",
            "This candidate already has an open invitation",
            invitation_id=str(open_row[0]),
        )
    inv = AssessmentInvitation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        assessment_id=assessment.id,
        assessment_version_id=assessment.current_version_id,
        candidate_id=req.candidate_id,
        application_id=req.application_id,
        context_kind=req.context_kind,
        context_id=req.context_id,
        status="invited",
        gating=assessment.gating_default if req.gating is None else req.gating,
        deadline_at=deadline,
        invited_by_kind=actor.kind,
        invited_by_id=actor.id,
        accommodation_factor=Decimal(str(req.accommodation_factor)),
        idempotency_key=req.idempotency_key,
    )
    try:
        async with db.begin_nested():
            db.add(inv)
            await db.flush()
    except IntegrityError as exc:
        raise conflict("invitation_exists", "This candidate already has an open invitation") from exc
    _emit_created(db, inv, actor.kind)
    if assessment.delivery == "provider":
        from app.services.assessments import provider

        await provider.queue_invite(ctx, db, inv, assessment)
    else:
        token = await issue_link(db, inv, s.link_grace_days, user_id(ctx))
        await db.flush()
        if req.send_notice:
            await send_invite_email(db, inv, assessment.name, token)
    return summary(inv)


async def get_invitation(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID
) -> AssessmentInvitationSummary:
    return summary(await load(db, invitation_id))


async def bulk_invite(
    ctx: RequestContext,
    db: AsyncSession,
    assessment_id: uuid.UUID,
    data: BulkInviteIn,
    pool_member_ids: list[uuid.UUID] | None = None,
) -> BulkInviteOut:
    """Loop ``invite`` (each in a savepoint) and report per-candidate results. Pool members are resolved by the router
    (``pools_service.member_ids``) -- this domain never imports ``talent_pools``."""
    from app.core.errors import DomainError
    from app.models.pipeline import Application

    targets: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    for cid in [*(data.candidate_ids or []), *(pool_member_ids or [])]:
        targets.append((cid, None))
    if data.application_ids:
        rows = await db.execute(
            select(Application.id, Application.candidate_id).where(Application.id.in_(data.application_ids))
        )
        found = dict(rows.tuples().all())
        targets += [(found[a], a) for a in data.application_ids if a in found]
    seen: set[uuid.UUID] = set()
    results: list[BulkResult] = []
    for cid, app_id in targets[:500]:
        if cid in seen:
            continue
        seen.add(cid)
        req = AssessmentInviteIn(
            assessment_id=assessment_id,
            candidate_id=cid,
            context_kind="application" if app_id else "direct",
            application_id=app_id,
            gating=data.gating,
            deadline_at=data.deadline_at,
            accommodation_factor=data.accommodation_factor,
        )
        try:
            async with db.begin_nested():
                out = await invite(ctx, db, req)
                if data.screen_reader_mode:
                    inv = await db.get(AssessmentInvitation, out.id)
                    if inv is not None:
                        inv.screen_reader_mode = True
                        await db.flush()
            results.append(
                BulkResult(
                    candidate_id=cid,
                    application_id=app_id,
                    status="created",
                    invitation_id=out.id,
                    error_code=None,
                )
            )
        except DomainError as exc:
            existing = exc.details.get("invitation_id") if isinstance(exc.details, dict) else None
            status = "exists" if exc.code == "invitation_exists" else "error"
            results.append(
                BulkResult(
                    candidate_id=cid, application_id=app_id, status=status,
                    invitation_id=uuid.UUID(existing) if existing else None, error_code=exc.code,
                )
            )  # fmt: skip
    return BulkInviteOut(results=results)


# ---------------------------------------------------------------- read models
async def hidden_scores(db: AsyncSession, invs: list[AssessmentInvitation]) -> dict[uuid.UUID, str]:
    """Epic 49 (additive): ``invitation id -> reason`` for invitations whose (candidate, demand) pair a registered
    AEDT score suppressor hides (alternative process requested / LL144 notice period)."""
    from app.models.pipeline import Application
    from app.services.matching.ports import suppressed_pairs

    app_ids = {i.application_id for i in invs if i.application_id and i.candidate_id}
    if not app_ids:
        return {}
    demand_of = dict(
        (await db.execute(select(Application.id, Application.demand_id).where(Application.id.in_(app_ids))))
        .tuples()
        .all()
    )
    pairs = {
        i.id: (i.candidate_id, demand_of[i.application_id])
        for i in invs
        if i.candidate_id and i.application_id in demand_of
    }
    hidden = await suppressed_pairs(db, list(pairs.values()))  # type: ignore[arg-type]
    return {iid: hidden[p] for iid, p in pairs.items() if p in hidden}  # type: ignore[index]


async def outs(db: AsyncSession, invs: list[AssessmentInvitation]) -> list[InvitationOut]:
    if not invs:
        return []
    ids = [i.id for i in invs]
    names = dict(
        (
            await db.execute(
                select(Assessment.id, Assessment.name).where(
                    Assessment.id.in_({i.assessment_id for i in invs})
                )
            )
        )
        .tuples()
        .all()
    )
    deliveries = dict(
        (
            await db.execute(
                select(Assessment.id, Assessment.delivery).where(
                    Assessment.id.in_({i.assessment_id for i in invs})
                )
            )
        )
        .tuples()
        .all()
    )
    cands = await candidate_names(db, [i.candidate_id for i in invs if i.candidate_id])
    attempts = dict(
        (
            await db.execute(
                select(Attempt.invitation_id, Attempt.id)
                .where(Attempt.invitation_id.in_(ids))
                .order_by(Attempt.attempt_no)
            )
        )
        .tuples()
        .all()
    )
    counts: dict[uuid.UUID, dict[str, int]] = {}
    if attempts:
        rows = await db.execute(
            select(Attempt.invitation_id, ProctoringSignal.kind, func.count())
            .join(ProctoringSignal, ProctoringSignal.attempt_id == Attempt.id)
            .where(Attempt.invitation_id.in_(ids))
            .group_by(Attempt.invitation_id, ProctoringSignal.kind)
        )
        for inv_id, kind, n in rows.tuples():
            counts.setdefault(inv_id, {})[kind] = int(n)
    pending = dict(
        (
            await db.execute(
                select(Attempt.invitation_id, func.count())
                .join(AttemptAnswer, AttemptAnswer.attempt_id == Attempt.id)
                .where(
                    Attempt.invitation_id.in_(ids),
                    AttemptAnswer.needs_review.is_(True),
                    AttemptAnswer.final_score.is_(None),
                )
                .group_by(Attempt.invitation_id)
            )
        )
        .tuples()
        .all()
    )
    hidden = await hidden_scores(db, invs)
    return [
        InvitationOut(
            id=i.id,
            assessment_id=i.assessment_id,
            assessment_name=names.get(i.assessment_id, ""),
            delivery=deliveries.get(i.assessment_id, "builtin"),
            candidate_id=i.candidate_id,
            candidate_name=cands.get(i.candidate_id) if i.candidate_id else None,
            application_id=i.application_id,
            context_kind=i.context_kind,
            context_id=i.context_id,
            status=i.status,
            gating=i.gating,
            result=i.result,
            score=None if i.id in hidden else i.score,
            scored_at=i.scored_at,
            deadline_at=i.deadline_at,
            percentile=None if i.id in hidden else i.percentile,
            points=None if i.id in hidden else i.points,
            max_points=None if i.id in hidden else i.max_points,
            section_scores={} if i.id in hidden else dict(i.section_scores or {}),
            skill_scores={} if i.id in hidden else dict(i.skill_scores or {}),
            score_hidden_reason=hidden.get(i.id),
            accommodation_factor=i.accommodation_factor,
            screen_reader_mode=i.screen_reader_mode,
            extended_count=i.extended_count,
            report_url=i.report_url,
            report_file_id=i.report_file_id,
            provider_ref=i.provider_ref,
            decided_at=i.decided_at,
            decision_note=i.decision_note,
            cancel_reason=i.cancel_reason,
            created_at=i.created_at,
            attempt_id=attempts.get(i.id),
            signal_counts=counts.get(i.id, {}),
            pending_reviews=int(pending.get(i.id, 0)),
        )
        for i in invs
    ]


async def detail(ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID) -> InvitationOut:
    return (await outs(db, [await load(db, invitation_id)]))[0]


async def list_invitations(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    assessment_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[InvitationOut]:
    stmt = select(AssessmentInvitation).where(AssessmentInvitation.candidate_id.is_not(None))
    if assessment_id:
        stmt = stmt.where(AssessmentInvitation.assessment_id == assessment_id)
    if candidate_id:
        stmt = stmt.where(AssessmentInvitation.candidate_id == candidate_id)
    if application_id:
        stmt = stmt.where(AssessmentInvitation.application_id == application_id)
    if status:
        stmt = stmt.where(AssessmentInvitation.status == status)
    rows = list(
        (await db.execute(stmt.order_by(AssessmentInvitation.created_at.desc()).limit(500))).scalars()
    )
    return await outs(db, rows)


async def candidate_results(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID
) -> list[InvitationOut]:
    from app.services.candidates import common as cand_common

    await cand_common.get_visible(ctx, db, candidate_id)  # 404 outside the caller's candidate read scope
    return await list_invitations(ctx, db, candidate_id=candidate_id)


async def badges(ctx: RequestContext, db: AsyncSession, application_ids: list[uuid.UUID]) -> BadgesOut:
    if not application_ids:
        return BadgesOut(items=[])
    rows = (
        await db.execute(
            select(AssessmentInvitation)
            .where(AssessmentInvitation.application_id.in_(application_ids[:200]))
            .order_by(AssessmentInvitation.application_id, AssessmentInvitation.created_at.desc())
            .distinct(AssessmentInvitation.application_id)
        )
    ).scalars()
    invs = list(rows)
    hidden = await hidden_scores(db, invs)
    return BadgesOut(
        items=[
            BadgeOut(
                application_id=i.application_id,
                invitation_id=i.id,
                status=i.status,
                score=None if i.id in hidden else i.score,
                result=i.result,
                score_hidden_reason=hidden.get(i.id),
            )  # type: ignore[arg-type]
            for i in invs
        ]
    )


# ---------------------------------------------------------------- user actions
async def patch(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID, data: InvitationPatch
) -> InvitationOut:
    inv = await load(db, invitation_id, for_update=True)
    if inv.status != "invited":
        raise conflict("invitation_started", "Accommodations can change only before the test starts")
    if data.screen_reader_mode is not None:
        inv.screen_reader_mode = data.screen_reader_mode
    if data.accommodation_factor is not None:
        inv.accommodation_factor = data.accommodation_factor
    await db.flush()
    return await detail(ctx, db, inv.id)


async def extend(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID, data: ExtendIn
) -> InvitationOut:
    actor = actor_of(ctx)
    inv = await load(db, invitation_id, for_update=True)
    if data.deadline_at <= now():
        raise invalid("deadline_in_past", "The deadline must be in the future", "deadline_at")
    if inv.status == "expired":
        await lifecycle.move(db, inv, "invited", actor.kind, actor_id=actor.id)
    elif inv.status != "invited":
        raise conflict(
            "transition_not_allowed",
            "Only invited or expired invitations can be extended",
            from_status=inv.status,
        )
    elif actor.kind != "user":
        lifecycle.require("expired", "invited", actor.kind)
    inv.deadline_at = data.deadline_at
    inv.extended_count += 1
    inv.reminder_sent_at = None
    assessment = await db.get(Assessment, inv.assessment_id)
    if assessment is not None and assessment.delivery == "builtin":
        s = await settings_mod.current(db)
        token = await issue_link(db, inv, s.link_grace_days, user_id(ctx))
        await db.flush()
        await send_invite_email(db, inv, assessment.name, token, extended=True)
    await db.flush()
    return await detail(ctx, db, inv.id)


async def cancel(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID, data: CancelIn
) -> InvitationOut:
    actor = actor_of(ctx)
    inv = await load(db, invitation_id, for_update=True)
    inv.cancel_reason = data.reason
    await lifecycle.move(db, inv, "cancelled", actor.kind, actor_id=actor.id)
    if inv.secure_link_id is not None:
        await secure_links.revoke(db, inv.secure_link_id)
    for att in (
        await db.execute(
            select(Attempt).where(Attempt.invitation_id == inv.id, Attempt.status == "in_progress")
        )
    ).scalars():
        att.status = "submitted"
        att.submitted_at = now()
    await db.flush()
    return await detail(ctx, db, inv.id)


async def decide(
    ctx: RequestContext, db: AsyncSession, invitation_id: uuid.UUID, data: DecideIn
) -> InvitationOut:
    """Pass / Fail (star: human only when gating). Never moves the pipeline."""
    actor = actor_of(ctx)
    inv = await load(db, invitation_id, for_update=True)
    target = "passed" if data.result == "pass" else "failed"
    if actor.kind not in lifecycle.HUMAN_DECIDERS:
        # Automated actors (ai / workflow / system / api_key) never decide through this path (section 5.1).
        lifecycle.require(inv.status, target, "__automated__")
    await lifecycle.move(db, inv, target, actor.kind, actor_id=actor.id, result=data.result)
    inv.decided_by = actor.id
    inv.decision_note = data.note
    await db.flush()
    return await detail(ctx, db, inv.id)


# ---------------------------------------------------------------- beats
async def expire_due(db: AsyncSession) -> int:
    rows = list(
        (
            await db.execute(
                select(AssessmentInvitation)
                .where(AssessmentInvitation.status == "invited", AssessmentInvitation.deadline_at < now())
                .with_for_update(skip_locked=True)
                .limit(1000)
            )
        ).scalars()
    )
    for inv in rows:
        await lifecycle.move(db, inv, "expired", "system")
        if inv.secure_link_id is not None:
            await secure_links.revoke(db, inv.secure_link_id)
    await db.flush()
    return len(rows)


async def send_reminders(db: AsyncSession) -> int:
    s = await settings_mod.current(db)
    horizon = now() + timedelta(hours=s.reminder_hours_before)
    rows = list(
        (
            await db.execute(
                select(AssessmentInvitation)
                .where(
                    AssessmentInvitation.status.in_(INVITATION_OPEN_STATUSES),
                    AssessmentInvitation.reminder_sent_at.is_(None),
                    AssessmentInvitation.deadline_at <= horizon,
                    AssessmentInvitation.deadline_at > now(),
                    AssessmentInvitation.candidate_id.is_not(None),
                )
                .with_for_update(skip_locked=True)
                .limit(1000)
            )
        ).scalars()
    )
    for inv in rows:
        inv.reminder_sent_at = now()
        emit(
            db,
            AssessmentReminderDue(
                tenant_id=inv.tenant_id, invitation_id=inv.id, candidate_id=inv.candidate_id
            ),
        )  # type: ignore[arg-type]
        email = await candidate_email(db, inv.candidate_id)  # type: ignore[arg-type]
        if email:
            name = (await db.get(Assessment, inv.assessment_id)).name  # type: ignore[union-attr]
            try:
                await _email(
                    inv, email, f"Reminder: assessment {name}",
                    f"Your assessment '{name}' is due by {inv.deadline_at.strftime('%d %b %Y %H:%M UTC')}. "
                    "Use the link in your invitation email, or your candidate portal, to start or resume it.",
                    "assessment_reminder",
                )  # fmt: skip
            except Exception:  # pragma: no cover - mail failures never block the beat
                logger.exception("assessment reminder email failed for {}", inv.id)
    await db.flush()
    return len(rows)


def details_of(inv: AssessmentInvitation) -> dict[str, Any]:
    return {"invitation_id": str(inv.id), "status": inv.status}


__all__ = [
    "badges",
    "bulk_invite",
    "cancel",
    "candidate_results",
    "decide",
    "detail",
    "expire_due",
    "extend",
    "get_invitation",
    "invite",
    "issue_link",
    "list_invitations",
    "load",
    "outs",
    "patch",
    "send_reminders",
    "summary",
]
