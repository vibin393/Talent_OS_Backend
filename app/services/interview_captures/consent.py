# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Capture consent and Recording Policy gating (Epic 60, story 60.1; FR-301, NFR-51).

A capture is offered only when:

* the Candidate has an active ``interview_recording`` consent (Secure Link from the invite email or the Candidate Portal);
* every internal interviewer (any non-candidate participant with a User) holds an active **standing** acknowledgement
  (once per tenant, revocable from "My recording consent");
* every external (Client) interviewer acknowledged **this** Interview through their per-interview Secure Link;
* the Recording Policy permits recording in the Candidate's confirmed jurisdiction (else the profile location) **and**
  in the tenant's own country (the interviewers' side, known gap 1).

FR-301 asks for every participant's consent whatever the rule, so all-participant acknowledgement is enforced on top of
the gate's ``candidate_consent`` rule (stricter, never looser). A transcript-only capture needs the same consent.
Blocked reasons: ``missing_consent:candidate``, ``missing_ack:<participant_id>``, ``not_permitted:<jurisdiction>``,
``unknown_jurisdiction``.
"""

import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound
from app.core.permissions import Principal, Scope
from app.models.candidates import Candidate
from app.models.interview_captures import InterviewCapture, InterviewCaptureAck, InterviewScorecardDraft
from app.models.interviews import Interview, InterviewParticipant
from app.models.legal_entities import LegalEntity
from app.models.privacy import Consent
from app.models.recording_policy import RecordingPolicy
from app.schemas.interview_captures import (
    CaptureStatusOut,
    ConsentRequestsOut,
    ParticipantConsentOut,
    RecordingAckOut,
)
from app.services.interview_captures import common
from app.services.interview_captures.common import (
    ACK_LINK_PURPOSE,
    CAPTURE_SUBJECT,
    CONSENT_LINK_PURPOSE,
    MANAGE,
    OPEN_STATUSES,
    PARTICIPANT_SUBJECT,
    PURPOSE,
    STATEMENT_VERSION,
    now,
)

ACK_STATEMENT = (
    "I agree to interviews I take part in being recorded or transcribed when everyone agrees. Only the words are used: "
    "the Interview Assistant turns the transcript into a summary and a private draft scorecard for each interviewer. "
    "You can withdraw at any time; recordings and transcripts of interviews not yet scored are then deleted."
)
EXTERNAL_STATEMENT = (
    "I agree to this interview being recorded or transcribed. Only the words are used, to prepare private notes for "
    "the interviewers. Recordings are kept for at most 90 days."
)
NOTICE_TITLE = "Recording or transcribing your interview"
NOTICE_BODY = (
    "With your agreement, and only where the law allows it, the interviewers may record or transcribe your interview.\n\n"
    "* **Who records:** an interviewer uploads the meeting recording (audio only) or the meeting transcript.\n"
    "* **What is used:** only the words. An AI assistant prepares a summary and draft notes that only your interviewers "
    "see; it never judges your voice, emotions, personality or any personal characteristic, and a person makes every "
    "decision.\n"
    "* **Sub-processor:** the AI model provider is a sub-processor that may process the transcript outside India.\n"
    "* **How long:** recordings are deleted after 90 days at the latest; transcripts follow the hiring record.\n"
    "* **Withdrawal:** you can withdraw at any time; the recording is then deleted within 24 hours and the transcript "
    "erased."
)


def _uid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


# ================================================================ policy evaluation
async def _rule_for(db: AsyncSession, country: str, region: str | None) -> str:
    rows = list(
        (await db.execute(select(RecordingPolicy).where(RecordingPolicy.country == country))).scalars()
    )
    if region is not None:
        for row in rows:
            if row.region is not None and row.region.upper() == region.upper():
                return row.rule
    for row in rows:
        if row.region is None:
            return row.rule
    return "not_permitted"


async def tenant_country(db: AsyncSession) -> str:
    """The tenant's own country (interviewers' side): its first active legal entity, else India (launch market)."""
    row = (
        await db.execute(
            select(LegalEntity.country)
            .where(LegalEntity.status == "active")
            .order_by(LegalEntity.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return (row or "IN").upper()


async def candidate_jurisdiction(
    db: AsyncSession, capture: InterviewCapture | None, candidate_id: uuid.UUID
) -> tuple[str | None, str | None]:
    from app.services.recording_policy.gate import normalize_country, normalize_region

    if capture is not None and capture.confirmed_country:
        return capture.confirmed_country, capture.confirmed_region
    loc = (
        await db.execute(select(Candidate.location).where(Candidate.id == candidate_id))
    ).scalar_one_or_none()
    if not isinstance(loc, Mapping):
        return None, None
    return normalize_country(loc.get("country")), normalize_region(loc.get("state"))


async def _candidate_consent(db: AsyncSession, candidate_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    rows = list(
        (
            await db.execute(
                select(Consent.id, Consent.status)
                .where(Consent.candidate_id == candidate_id, Consent.purpose == PURPOSE)
                .order_by(Consent.created_at.desc())
            )
        ).all()
    )
    for cid, status in rows:
        if status == "granted":
            return "agreed", cid
    return ("refused", None) if rows else ("waiting", None)


async def standing_acks(db: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, InterviewCaptureAck]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(InterviewCaptureAck).where(
                InterviewCaptureAck.scope == "standing",
                InterviewCaptureAck.revoked_at.is_(None),
                InterviewCaptureAck.user_id.in_(user_ids),
            )
        )
    ).scalars()
    return {r.user_id: r for r in rows if r.user_id is not None}


async def interview_acks(db: AsyncSession, interview_id: uuid.UUID) -> dict[uuid.UUID, InterviewCaptureAck]:
    rows = (
        await db.execute(
            select(InterviewCaptureAck).where(
                InterviewCaptureAck.scope == "interview",
                InterviewCaptureAck.revoked_at.is_(None),
                InterviewCaptureAck.interview_id == interview_id,
            )
        )
    ).scalars()
    return {r.interview_participant_id: r for r in rows if r.interview_participant_id is not None}


class Evaluation:
    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.participants: list[ParticipantConsentOut] = []
        self.jurisdiction: str | None = None
        self.rule: str | None = None
        self.consent_id: uuid.UUID | None = None
        self.acks: list[dict[str, Any]] = []
        self.policy_blocked = False

    @property
    def permitted(self) -> bool:
        return not self.reasons


async def evaluate(db: AsyncSession, interview: Interview, capture: InterviewCapture | None) -> Evaluation:
    """Pure read: participants' state, the gate decision and the blocked reasons (no writes)."""
    from app.services import entity_refs

    ev = Evaluation()
    country, region = await candidate_jurisdiction(db, capture, interview.candidate_id)
    if country is None:
        ev.reasons.append("unknown_jurisdiction")
        ev.policy_blocked = True
        ev.rule = "unknown"
    else:
        label = f"{country}-{region}" if region else country
        ev.jurisdiction = label
        ev.rule = await _rule_for(db, country, region)
        if ev.rule == "not_permitted":
            ev.reasons.append(f"not_permitted:{label}")
            ev.policy_blocked = True
    own = await tenant_country(db)
    if own != country and await _rule_for(db, own, None) == "not_permitted":
        ev.reasons.append(f"not_permitted:{own}")
        ev.policy_blocked = True
    state, consent_id = await _candidate_consent(db, interview.candidate_id)
    ev.consent_id = consent_id
    cand_name = (await entity_refs.entity_label(db, "candidate", interview.candidate_id)) or "Candidate"
    ev.participants.append(
        ParticipantConsentOut(participant_id=None, kind="candidate", name=cand_name, state=state)
    )
    if state != "agreed":
        ev.reasons.append("missing_consent:candidate")
    parts = await common.participants(db, interview.id)
    internal, external = common.interviewers(parts)
    users = await entity_refs.users(db, [p.user_id for p in internal if p.user_id])
    standing = await standing_acks(db, [p.user_id for p in internal if p.user_id])
    for p in internal:
        ack = standing.get(p.user_id)  # type: ignore[arg-type]
        name = p.name or (users[p.user_id].name if p.user_id in users else "Interviewer")
        ev.participants.append(
            ParticipantConsentOut(
                participant_id=p.id,
                kind="internal",
                name=name,
                user_id=p.user_id,
                state="agreed" if ack else "waiting",
            )
        )
        if ack is None:
            ev.reasons.append(f"missing_ack:{p.id}")
        else:
            ev.acks.append({"kind": "user", "id": str(p.user_id), "acked_at": ack.acked_at.isoformat()})
    per_interview = await interview_acks(db, interview.id)
    for p in external:
        ack = per_interview.get(p.id)
        ev.participants.append(
            ParticipantConsentOut(
                participant_id=p.id,
                kind="external",
                name=p.name or p.email,
                state="agreed" if ack else "waiting",
            )
        )
        if ack is None:
            ev.reasons.append(f"missing_ack:{p.id}")
        else:
            ev.acks.append({"kind": "external", "id": str(p.id), "acked_at": ack.acked_at.isoformat()})
    return ev


def reason_texts(reasons: list[str], participants: list[ParticipantConsentOut]) -> list[str]:
    names = {str(p.participant_id): p.name for p in participants if p.participant_id is not None}
    out: list[str] = []
    for r in reasons:
        if r == "missing_consent:candidate":
            out.append("Waiting for the candidate to agree")
        elif r.startswith("missing_ack:"):
            out.append(f"Waiting for {names.get(r.split(':', 1)[1], 'an interviewer')} to agree")
        elif r.startswith("not_permitted:"):
            out.append(f"Recording isn't permitted in {r.split(':', 1)[1]} under your recording policy")
        elif r == "unknown_jurisdiction":
            out.append("The candidate's location is unknown; ask them to confirm where they are")
        else:
            out.append(r)
    return out


def _apply(capture: InterviewCapture, ev: Evaluation) -> None:
    capture.blocked_reasons = list(ev.reasons)
    capture.jurisdiction = ev.jurisdiction
    capture.recording_rule = (
        ev.rule if ev.rule in ("not_permitted", "candidate_consent", "all_party_consent") else None
    )
    capture.recording_consent_id = ev.consent_id
    capture.participant_acks = ev.acks
    capture.recording_allowed = ev.permitted or capture.file_id is not None
    if capture.status in OPEN_STATUSES:
        capture.status = (
            "planned" if ev.permitted else ("blocked" if ev.policy_blocked else "consent_pending")
        )


async def refresh(db: AsyncSession, interview: Interview, capture: InterviewCapture) -> Evaluation:
    """Re-evaluate and store the capture's gate state (status only changes while the capture is still open)."""
    ev = await evaluate(db, interview, capture)
    _apply(capture, ev)
    await db.flush()
    return ev


# ================================================================ access
def _has(principal: Principal, perm: str) -> bool:
    return perm in (principal.permissions or {})


async def is_participant(db: AsyncSession, interview: Interview, user_id: uuid.UUID | None) -> bool:
    if user_id is None:
        return False
    if interview.organizer_user_id == user_id:
        return True
    return any(p.user_id == user_id for p in await common.participants(db, interview.id))


async def readable_interview(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> Interview:
    """404 unless the caller may read the Interview (P1 rule: participant, or ``interview:read`` in scope)."""
    from app.services import interviews_service

    await interviews_service.get_interview(ctx, db, interview_id)
    return await common.interview_row(db, interview_id)


async def manageable_interview(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> Interview:
    """``interview_capture:manage`` and (participant or ``interview:schedule``) -> else 403 / 404."""
    interview = await readable_interview(ctx, db, interview_id)
    principal = common.principal_of(ctx)
    if not _has(principal, MANAGE):
        raise Forbidden("You can't manage interview capture", code="forbidden")
    if not await is_participant(db, interview, principal.id) and not _has(principal, "interview:schedule"):
        raise Forbidden(
            "Only the interview's participants or schedulers can manage capture", code="not_a_participant"
        )
    return interview


async def can_manage(ctx: RequestContext, db: AsyncSession, interview: Interview) -> bool:
    principal = ctx.principal
    if not isinstance(principal, Principal) or not _has(principal, MANAGE):
        return False
    return await is_participant(db, interview, principal.id) or _has(principal, "interview:schedule")


# ================================================================ status / request
async def status_out(
    ctx: RequestContext, db: AsyncSession, interview: Interview, capture: InterviewCapture | None
) -> CaptureStatusOut:
    from app.models.interview_captures import InterviewCaptureOutput

    settings = await common.settings_row(db)
    ev = await evaluate(db, interview, capture)
    reasons = list(ev.reasons)
    user_id = common.principal_of(ctx).id
    has_outputs = has_draft = False
    if capture is not None:
        has_outputs = (
            await db.execute(
                select(InterviewCaptureOutput.id).where(
                    InterviewCaptureOutput.capture_id == capture.id, InterviewCaptureOutput.status == "ready"
                )
            )
        ).first() is not None
        has_draft = (
            await db.execute(
                select(InterviewScorecardDraft.id).where(
                    InterviewScorecardDraft.capture_id == capture.id,
                    InterviewScorecardDraft.user_id == user_id,
                    InterviewScorecardDraft.status != "superseded",
                )
            )
        ).first() is not None
    status = capture.status if capture is not None else "not_requested"
    return CaptureStatusOut(
        interview_id=interview.id,
        capture_id=capture.id if capture else None,
        status=status,
        capture_kind=capture.capture_kind if capture else None,
        offered=ev.permitted
        and interview.mode in common.CAPTURABLE_MODES
        and interview.status in common.CAPTURABLE_STATUSES,
        reasons=reasons,
        reason_texts=reason_texts(reasons, ev.participants),
        participants=ev.participants,
        jurisdiction=ev.jurisdiction,
        recording_rule=ev.rule,
        uploaded_at=capture.uploaded_at if capture else None,
        unknown_speakers=[str(s) for s in (capture.speakers or [])] if capture else [],
        failure_code=capture.failure_code if capture else None,
        run_id=capture.agent_run_id if capture else None,
        has_outputs=has_outputs,
        has_draft=has_draft,
        can_manage=await can_manage(ctx, db, interview),
        allow_recording_upload=settings.allow_recording_upload,
        allow_transcript_upload=settings.allow_transcript_upload,
    )


async def capture_status(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> CaptureStatusOut:
    interview = await readable_interview(ctx, db, interview_id)
    capture = await common.capture_for_interview(db, interview_id)
    if capture is not None and capture.status in OPEN_STATUSES:
        await refresh(db, interview, capture)
    return await status_out(ctx, db, interview, capture)


def _capturable(interview: Interview) -> None:
    if interview.mode not in common.CAPTURABLE_MODES:
        raise common.conflict("capture_not_available", "This interview's mode can't be captured")
    if interview.status not in common.CAPTURABLE_STATUSES:
        raise common.conflict(
            "capture_not_available", "Only scheduled or completed interviews can be captured"
        )


async def ensure_capture(ctx: RequestContext, db: AsyncSession, interview: Interview) -> InterviewCapture:
    capture = await common.capture_for_interview(db, interview.id, for_update=True)
    if capture is None:
        capture = InterviewCapture(
            id=uuid.uuid4(),
            tenant_id=interview.tenant_id,
            interview_id=interview.id,
            candidate_id=interview.candidate_id,
            status="consent_pending",
            requested_by=common.principal_of(ctx).id,
            participant_acks=[],
            blocked_reasons=[],
            speakers=[],
            recording_allowed=False,
        )
        db.add(capture)
        await db.flush()
        audit.record(
            "interview_capture.requested", "interview", interview.id, session=db, capture_id=str(capture.id)
        )
    elif capture.status == "erased":
        raise common.conflict("capture_erased", "This interview's capture was erased after a withdrawal")
    return capture


async def request_capture(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID
) -> ConsentRequestsOut:
    interview = await manageable_interview(ctx, db, interview_id)
    _capturable(interview)
    capture = await ensure_capture(ctx, db, interview)
    ev = await refresh(db, interview, capture)
    sent = (
        await _send_missing(ctx, db, interview, capture, ev)
        if not any(r.startswith("not_permitted:") for r in ev.reasons)
        else []
    )
    if capture.status == "blocked":
        await _notify_blocked(db, interview, capture)
    return ConsentRequestsOut(status=await status_out(ctx, db, interview, capture), sent=sent)


async def request_acks(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> ConsentRequestsOut:
    interview = await manageable_interview(ctx, db, interview_id)
    _capturable(interview)
    capture = await ensure_capture(ctx, db, interview)
    ev = await refresh(db, interview, capture)
    sent = (
        await _send_missing(ctx, db, interview, capture, ev)
        if not any(r.startswith("not_permitted:") for r in ev.reasons)
        else []
    )
    return ConsentRequestsOut(status=await status_out(ctx, db, interview, capture), sent=sent)


async def _link_days(db: AsyncSession) -> int:
    return int((await common.settings_row(db)).consent_link_days)


async def candidate_link(db: AsyncSession, capture: InterviewCapture, created_by: uuid.UUID | None) -> str:
    _, token = await secure_links.issue(
        db, tenant_id=capture.tenant_id, purpose=CONSENT_LINK_PURPOSE, subject_type=CAPTURE_SUBJECT,
        subject_id=capture.id, ttl=timedelta(days=await _link_days(db)), max_uses=20, created_by=created_by,
    )  # fmt: skip
    return secure_links.link_url(token)


async def _send_missing(
    ctx: RequestContext, db: AsyncSession, interview: Interview, capture: InterviewCapture, ev: Evaluation
) -> list[str]:
    """Templated consent requests for whoever has not agreed yet (Candidate email, external interviewer links,
    in-app requests to internal interviewers). Returns what was sent."""
    from app.core import mailer
    from app.services import entity_refs

    sent: list[str] = []
    tenant = await entity_refs.tenant(db, interview.tenant_id)
    me = common.principal_of(ctx).id
    when = interview.starts_at.astimezone(tenant.tz).strftime("%d %b %Y %H:%M")
    if "missing_consent:candidate" in ev.reasons:
        who = await entity_refs.addressee(db, "candidate", interview.candidate_id)
        state = next((p.state for p in ev.participants if p.kind == "candidate"), "waiting")
        if who is not None and who.address and state != "refused":
            url = await candidate_link(db, capture, me)
            first = entity_refs.first_name(who.full_name) or "there"
            await _mail(
                mailer.EmailMessage(
                    to=who.address, subject=f"Recording your interview with {tenant.name}",
                    text=(
                        f"Hello {first},\n\nYour {interview.stage_name} interview is on {when} ({tenant.timezone}). "
                        "The interviewers would like to record or transcribe it so they can focus on the conversation. "
                        "Only the words are used and a person makes every decision.\n\n"
                        f"Please tell us where you are and whether you agree:\n{url}\n\nYou can say no; the interview goes "
                        f"ahead as usual.\n\n{tenant.name}"
                    ),
                    tenant_id=interview.tenant_id, kind="interview_recording_consent", sender_user_id=me,
                    headers={"X-TOS-Interview-Id": str(interview.id)},
                )
            )  # fmt: skip
            sent.append("candidate")
    parts = {p.id: p for p in await common.participants(db, interview.id)}
    for reason in ev.reasons:
        if not reason.startswith("missing_ack:"):
            continue
        p = parts.get(_uid(reason.split(":", 1)[1]))  # type: ignore[arg-type]
        if p is None:
            continue
        if p.user_id is None:
            _, token = await secure_links.issue(
                db, tenant_id=interview.tenant_id, purpose=ACK_LINK_PURPOSE, subject_type=PARTICIPANT_SUBJECT,
                subject_id=p.id, ttl=timedelta(days=await _link_days(db)), max_uses=5, created_by=me,
            )  # fmt: skip
            await _mail(
                mailer.EmailMessage(
                    to=p.email, subject=f"Agree to recording for the {interview.stage_name} interview",
                    text=(
                        f"Hello {p.name or 'there'},\n\n{tenant.name} would like to record or transcribe the {interview.stage_name} "
                        f"interview on {when} ({tenant.timezone}). Everyone taking part must agree first.\n\n"
                        f"Agree to recording for this interview:\n{secure_links.link_url(token)}\n\n{tenant.name}"
                    ),
                    tenant_id=interview.tenant_id, kind="interview_capture_ack_requested", sender_user_id=me,
                    headers={"X-TOS-Interview-Id": str(interview.id)},
                )
            )  # fmt: skip
            sent.append(f"external:{p.id}")
        else:
            _fan_out(interview.tenant_id, "interview_capture_ack_requested", {
                "event_id": f"ia_ack_req:{interview.id}:{p.user_id}:{now().date()}", "interview_id": str(interview.id),
                "user_id": str(p.user_id),
            })  # fmt: skip
            sent.append(f"internal:{p.id}")
    audit.record("interview_capture.consent_requested", "interview", interview.id, session=db, sent=sent)
    return sent


async def _mail(message: Any) -> None:
    from app.core import mailer

    try:
        await mailer.send_routed(message)
    except Exception:
        logger.exception("Interview capture consent email failed")


def _fan_out(tenant_id: uuid.UUID, type_key: str, payload: dict[str, Any]) -> None:
    from app.tasks import notifications as notification_tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(notification_tasks.fan_out, str(tenant_id), type_key, payload)
    except Exception:
        logger.exception("Interview capture notification {} failed", type_key)


async def _notify_blocked(db: AsyncSession, interview: Interview, capture: InterviewCapture) -> None:  # ruff: ignore[unused-function-argument]
    if capture.requested_by is None:
        return
    _fan_out(interview.tenant_id, "interview_capture_blocked", {
        "event_id": f"ia_blocked:{capture.id}:{','.join(sorted(capture.blocked_reasons or []))}"[:200],
        "interview_id": str(interview.id), "user_id": str(capture.requested_by),
        "reason": (capture.blocked_reasons or ["not_permitted"])[0],
    })  # fmt: skip


# ================================================================ internal interviewers (standing ack)
async def my_ack(ctx: RequestContext, db: AsyncSession) -> RecordingAckOut:
    principal = common.principal_of(ctx)
    ack = (await standing_acks(db, [principal.id])).get(principal.id)
    return RecordingAckOut(
        acked=ack is not None, acked_at=ack.acked_at if ack else None, statement=ACK_STATEMENT,
        statement_version=STATEMENT_VERSION,
    )  # fmt: skip


async def give_ack(ctx: RequestContext, db: AsyncSession) -> RecordingAckOut:
    principal = common.principal_of(ctx)
    if principal.type != "user":
        raise Forbidden("Only a signed-in user can agree", code="human_only_action")
    if (await standing_acks(db, [principal.id])).get(principal.id) is None:
        db.add(
            InterviewCaptureAck(
                id=uuid.uuid4(), tenant_id=ctx.tenant_id, participant_kind="user", user_id=principal.id,
                scope="standing", acked_at=now(), statement_version=STATEMENT_VERSION,
            )
        )  # fmt: skip
        await db.flush()
        audit.record("interview_capture.ack_given", "user", principal.id, session=db, scope="standing")
        await _refresh_for_user(db, principal.id)
    return await my_ack(ctx, db)


async def revoke_ack(ctx: RequestContext, db: AsyncSession) -> RecordingAckOut:
    principal = common.principal_of(ctx)
    ack = (await standing_acks(db, [principal.id])).get(principal.id)
    if ack is not None:
        ack.revoked_at = now()
        await db.flush()
        audit.record("interview_capture.ack_revoked", "user", principal.id, session=db, scope="standing")
        await _withdraw_for_user(db, principal.id)
    return await my_ack(ctx, db)


async def _captures_of_user(db: AsyncSession, user_id: uuid.UUID) -> list[tuple[Interview, InterviewCapture]]:
    rows = (
        await db.execute(
            select(Interview, InterviewCapture)
            .join(InterviewCapture, InterviewCapture.interview_id == Interview.id)
            .join(InterviewParticipant, InterviewParticipant.interview_id == Interview.id)
            .where(InterviewParticipant.user_id == user_id, InterviewParticipant.removed_at.is_(None))
        )
    ).all()
    return [(i, c) for i, c in rows]


async def _refresh_for_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    for interview, capture in await _captures_of_user(db, user_id):
        if capture.status in OPEN_STATUSES:
            await refresh(db, interview, capture)


async def _withdraw_for_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    from app.services.interview_captures import withdrawal

    for interview, capture in await _captures_of_user(db, user_id):
        if capture.status in OPEN_STATUSES:
            await refresh(db, interview, capture)
        elif capture.status != "erased":
            withdrawal.schedule(capture.tenant_id, capture.id, reason="ack_revoked")


# ================================================================ external interviewers (per-interview link)
async def _ack_link(token: str) -> secure_links.ResolvedLink:
    link = await secure_links.resolve(token, (ACK_LINK_PURPOSE,))
    if link.subject_type != PARTICIPANT_SUBJECT:
        raise NotFound("Link not found", code="link_invalid")
    return link


async def external_ack_view(token: str) -> dict[str, Any]:
    from app.core.database import tenant_session
    from app.services import entity_refs

    link = await _ack_link(token)
    async with tenant_session(link.tenant_id) as db:
        p = await db.get(InterviewParticipant, link.subject_id)
        if p is None or p.removed_at is not None:
            raise NotFound("Link not found", code="link_invalid")
        interview = await common.interview_row(db, p.interview_id)
        tenant = await entity_refs.tenant(db, link.tenant_id)
        acked = (await interview_acks(db, interview.id)).get(p.id) is not None
        return {
            "tenant_name": tenant.name, "participant_name": p.name, "stage_name": interview.stage_name,
            "starts_at": interview.starts_at, "statement": EXTERNAL_STATEMENT, "status": "agreed" if acked else "waiting",
        }  # fmt: skip


async def external_ack(token: str) -> dict[str, Any]:
    from app.core.database import tenant_session

    link = await _ack_link(token)
    async with tenant_session(link.tenant_id) as db:
        p = await db.get(InterviewParticipant, link.subject_id)
        if p is None or p.removed_at is not None or p.user_id is not None:
            raise NotFound("Link not found", code="link_invalid")
        interview = await common.interview_row(db, p.interview_id)
        if (await interview_acks(db, interview.id)).get(p.id) is None:
            await secure_links.consume(db, link.id)
            db.add(
                InterviewCaptureAck(
                    id=uuid.uuid4(), tenant_id=link.tenant_id, participant_kind="external", interview_id=interview.id,
                    interview_participant_id=p.id, scope="interview", acked_at=now(), secure_link_id=link.id,
                    statement_version=STATEMENT_VERSION,
                )
            )  # fmt: skip
            await db.flush()
            audit.record(
                "interview_capture.ack_given", "interview", interview.id, session=db, actor_type="system", actor_id=None,
                scope="interview", participant_id=str(p.id),
            )  # fmt: skip
            capture = await common.capture_for_interview(db, interview.id, for_update=True)
            if capture is not None:
                await refresh(db, interview, capture)
    return await external_ack_view(token)


# ================================================================ Candidate consent (Secure Link / portal)
async def _consent_link(token: str) -> secure_links.ResolvedLink:
    link = await secure_links.resolve(token, (CONSENT_LINK_PURPOSE,))
    if link.subject_type != CAPTURE_SUBJECT:
        raise NotFound("Link not found", code="link_invalid")
    return link


async def _consent_view(db: AsyncSession, capture: InterviewCapture) -> dict[str, Any]:
    from app.services import entity_refs, privacy

    interview = await common.interview_row(db, capture.interview_id)
    tenant = await entity_refs.tenant(db, capture.tenant_id)
    state, _ = await _candidate_consent(db, capture.candidate_id)
    notice = await privacy.consent.active_notice(db, capture.tenant_id, PURPOSE)
    who = await entity_refs.addressee(db, "candidate", capture.candidate_id)
    country, region = await candidate_jurisdiction(db, capture, capture.candidate_id)
    return {
        "tenant_name": tenant.name, "candidate_first_name": entity_refs.first_name(who.full_name if who else None),
        "stage_name": interview.stage_name, "starts_at": interview.starts_at, "status": state,
        "notice_title": notice.title if notice is not None else NOTICE_TITLE,
        "notice_body": notice.body_md if notice is not None else NOTICE_BODY, "country": country, "region": region,
    }  # fmt: skip


async def candidate_consent_view(token: str) -> dict[str, Any]:
    from app.core.database import tenant_session

    link = await _consent_link(token)
    async with tenant_session(link.tenant_id) as db:
        capture = await common.capture_row(db, link.subject_id)
        return await _consent_view(db, capture)


async def record_candidate_decision(
    db: AsyncSession,
    capture: InterviewCapture,
    decision: str,
    country: str | None,
    region: str | None,
    *,
    via: str,
) -> None:
    """The Candidate grants or refuses ``interview_recording`` (source ``interview_invite``) and confirms location."""
    from app.services import privacy
    from app.services.recording_policy.gate import normalize_country, normalize_region

    if capture.status == "erased":
        raise common.conflict("capture_erased", "This interview's recording was already deleted")
    ctx = RequestContext(tenant_id=capture.tenant_id, actor_kind="system")
    if decision == "agree":
        code = normalize_country(country)
        if code is None:
            raise common.invalid(
                "country_required", "Tell us which country you are in", fields={"country": "Required"}
            )
        capture.confirmed_country = code
        capture.confirmed_region = normalize_region(region)
        if not await privacy.consent.has_active_consent(db, capture.candidate_id, PURPOSE):
            await privacy.consent.grant_consent(
                ctx, db, capture.candidate_id, PURPOSE, source=common.CONSENT_SOURCE,
                evidence={"via": via, "capture_id": str(capture.id), "country": code, "region": capture.confirmed_region},
            )  # fmt: skip
    elif await privacy.consent.has_active_consent(db, capture.candidate_id, PURPOSE):
        await privacy.consent.withdraw_consent(ctx, db, capture.candidate_id, PURPOSE, by_kind="candidate")
    else:
        from app.models.privacy import Consent as ConsentRow

        db.add(
            ConsentRow(
                id=uuid.uuid4(), tenant_id=capture.tenant_id, candidate_id=capture.candidate_id, purpose=PURPOSE,
                status="withdrawn", source=common.CONSENT_SOURCE, withdrawn_at=now(), withdrawn_by_kind="candidate",
            )
        )  # fmt: skip
        await db.flush()
    interview = await common.interview_row(db, capture.interview_id)
    await refresh(db, interview, capture)


async def candidate_consent_decide(
    token: str, decision: str, country: str | None, region: str | None
) -> dict[str, Any]:
    from app.core.database import tenant_session

    link = await _consent_link(token)
    async with tenant_session(link.tenant_id) as db:
        capture = await common.capture_row(db, link.subject_id, for_update=True)
        await record_candidate_decision(db, capture, decision, country, region, via="secure_link")
        return await _consent_view(db, capture)


async def portal_captures(
    db: AsyncSession, candidate_id: uuid.UUID, interview_id: uuid.UUID
) -> InterviewCapture:
    capture = await common.capture_for_interview(db, interview_id, for_update=True)
    if capture is None or capture.candidate_id != candidate_id:
        raise NotFound("Interview not found")
    return capture


async def portal_view(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, interview_id: uuid.UUID
) -> dict[str, Any]:
    from app.core.database import tenant_session

    async with tenant_session(tenant_id) as db:
        return await _consent_view(db, await portal_captures(db, candidate_id, interview_id))


async def portal_decide(
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    interview_id: uuid.UUID,
    decision: str,
    country: str | None,
    region: str | None,
) -> dict[str, Any]:
    from app.core.database import tenant_session

    async with tenant_session(tenant_id) as db:
        capture = await portal_captures(db, candidate_id, interview_id)
        await record_candidate_decision(db, capture, decision, country, region, via="portal")
        return await _consent_view(db, capture)


__all__ = [
    "ACK_STATEMENT",
    "NOTICE_BODY",
    "NOTICE_TITLE",
    "Scope",
    "capture_status",
    "evaluate",
    "give_ack",
    "my_ack",
    "refresh",
    "request_acks",
    "request_capture",
    "revoke_ack",
]
