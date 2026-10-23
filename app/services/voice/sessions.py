# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Voice sessions (Epic 58, stories 58.1 / 58.2 / 58.5): request, read, cancel, speaker labels, the hand-offs to the
Voice Screening Agent and the review completion.

* ``request`` -- one open session per Application (409 ``voice_session_open``); refused for closed Applications
  (``application_closed``) and when the Candidate asked for a non-AI process (``alternative_process_requested``). The
  questionnaire comes from Epic 57's ``resolve_for_application``. ``mode=upload`` with ``voice_screening`` consent
  already on record -> ``planned`` (upload allowed); otherwise ``consent_pending`` and the agent's ``requested`` run
  sends the templated consent link (without an agent run the domain sends it itself).
* ``after_consent`` (``mode=call``) -- the agent's ``consented`` run schedules the call (domain fallback: schedule now).
* ``mark_ready`` -- a candidate-labelled transcript is ready: ``processing``, ``VoiceSessionReady`` and the agent's
  ``session_ready`` run (fallback: extract now and hand the answers to the recruiter).
* ``complete_review`` -- human only (★ ``voice_session`` -> ``reviewed``): Epic 57's ``complete_review`` writes the
  P1 ``screenings`` row; the outcome is then set through the unchanged P1 endpoint.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.voice import VoiceSessionReady
from app.models.pipeline import Application
from app.models.voice import OPEN_STATUSES, VoiceSession
from app.schemas.voice import VoiceSessionOut, VoiceTranscriptOut
from app.services.voice import common
from app.services.voice.common import PURPOSE_CALL

CLOSED = ("reviewed", "cancelled", "failed")


async def readable_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> Application:
    from app.services.pipeline.common import READ, application_for

    return await application_for(ctx, db, application_id, READ)


async def readable(
    ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID, *, for_update: bool = False
) -> VoiceSession:
    """A session whose Application the caller can read (else 404, also cross-tenant)."""
    row = await common.session_row(db, session_id, for_update=for_update)
    if row.application_id is None:
        raise NotFound("Voice session not found")
    try:
        await readable_application(ctx, db, row.application_id)
    except NotFound as exc:
        raise NotFound("Voice session not found") from exc
    return row


async def _closed(db: AsyncSession, app: Application) -> bool:
    from app.models.pipeline import PipelineStage
    from app.services.pipeline.stages import TERMINAL

    if app.archived_at is not None:
        return True
    stage = await db.get(PipelineStage, app.stage_id)
    return stage is not None and stage.key in TERMINAL


async def _alternative_process(db: AsyncSession, app: Application) -> bool:
    from app.services.matching.ports import suppressed_pairs

    hidden = await suppressed_pairs(db, [(app.candidate_id, app.demand_id)])
    return hidden.get((app.candidate_id, app.demand_id)) == "alternative_process"


async def _provider(db: AsyncSession, provider_key: str | None) -> str:
    from app.core.settings import get_settings
    from app.services.connectors.registry import adapter_factory

    key = provider_key or (await common.settings_row(db)).default_provider
    if key == "fake" and (get_settings().app_env == "production" or adapter_factory("voice", "fake") is None):
        raise common.invalid("provider_not_available", "Demo calls are not available here", provider_key=key)
    if adapter_factory("voice", key) is None:
        raise common.invalid("provider_not_available", "This call method is not available", provider_key=key)
    return key


async def request(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    mode: str = "call",
    provider_key: str | None = None,
    script_ref: str | None = None,
) -> VoiceSessionOut:
    from app.core.sequences import next_human_id
    from app.services import screening_agent_service

    app = await readable_application(ctx, db, application_id)
    if await _closed(db, app):
        raise common.conflict("application_closed", "The application is closed")
    open_row = (
        await db.execute(
            select(VoiceSession.id).where(
                VoiceSession.application_id == app.id, VoiceSession.status.in_(OPEN_STATUSES)
            )
        )
    ).first()
    if open_row is not None:
        raise common.conflict(
            "voice_session_open", "A voice screen is already in progress for this application"
        )
    if await _alternative_process(db, app):
        raise common.conflict("alternative_process_requested", "The candidate asked for a non-AI process")
    key = await _provider(db, provider_key)
    version = await screening_agent_service.resolve_for_application(ctx, db, app.id)
    consented = await common.has_consent(db, app.candidate_id, PURPOSE_CALL)
    row = VoiceSession(
        id=uuid.uuid4(), tenant_id=app.tenant_id, human_id=await next_human_id(db, "voice_session", "VS"),
        candidate_id=app.candidate_id, application_id=app.id, mode=mode, provider_key=key,
        status="planned" if (mode == "upload" and consented) else "consent_pending",
        requested_by=common.user_id_of(ctx), owner_user_id=app.owner_id, script_ref=script_ref if key == "fake" else None,
        questionnaire_id=version.questionnaire_id if version is not None else None,
        questionnaire_version=version.version if version is not None else None, recording_allowed=False, gaps=[],
    )  # fmt: skip
    if consented:
        row.voice_consent_id = await common.active_consent_id(db, app.candidate_id, PURPOSE_CALL)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    audit.record("voice_session.requested", "voice_session", row.id, session=db, mode=mode, provider_key=key)
    await common.timeline(
        ctx, db, row, f"Voice screen requested ({'call' if mode == 'call' else 'upload'})", "requested"
    )
    if row.status == "consent_pending":
        ref = await start_agent(ctx, db, row, "request")
        if ref is None:
            try:
                async with db.begin_nested():
                    from app.services.voice import consent

                    await consent.send_request(ctx, db, row, channel="email", origin="user")
            except Exception as exc:  # the recruiter can still upload once consent exists
                logger.info("Voice consent request not sent for {}: {}", row.id, exc)
    elif mode == "call":
        await after_consent(ctx, db, row)
    return await common.out(db, row)


# ================================================================ agent hand-offs
async def start_agent(ctx: RequestContext, db: AsyncSession, row: VoiceSession, stage: str) -> Any:
    """Start the Voice Screening Agent for ``stage`` (``request | consented | ready``). None when it did not start."""
    from app.core.agentic import Kickoff
    from app.services import agent_runtime_service

    if row.application_id is None:
        return None
    trigger = {"request": "requested", "consented": "consented", "ready": "session_ready"}[stage]
    inputs = {
        "voice_session_id": str(row.id), "application_id": str(row.application_id), "mode": row.mode,
        "provider_key": row.provider_key, "stage": stage, "questionnaire_id": str(row.questionnaire_id or ""),
        "questionnaire_version": str(row.questionnaire_version or ""),
    }  # fmt: skip
    try:
        async with db.begin_nested():
            ref = await agent_runtime_service.start_run(
                RequestContext(tenant_id=row.tenant_id, actor_kind="system") if stage != "request" else ctx, db,
                agent_key=common.AGENT_KEY, trigger_kind="event" if stage == "ready" else "manual", trigger_key=trigger,
                subject=("application", row.application_id), kickoff=Kickoff(inputs=inputs, candidate_ids=(row.candidate_id,)),
                owner_user_id=row.owner_user_id, dedup_key=f"voice:{stage}:{row.id}",
            )  # fmt: skip
    except Exception:
        logger.exception("Voice Screening Agent not started for {}", row.id)
        return None
    if ref.run_id is None:
        return None
    row.agent_run_id = ref.run_id
    await db.flush()
    return ref


async def after_consent(ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> None:
    from app.services.voice import provider_jobs

    if await start_agent(ctx, db, row, "consented") is None:
        try:
            async with db.begin_nested():
                await provider_jobs.schedule(ctx, db, row)
        except Exception:
            logger.exception("Voice call not scheduled for {}", row.id)


async def mark_ready(ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> None:
    """The transcript is ready with candidate turns: ``processing`` + ``VoiceSessionReady`` + the processing run."""
    row.status = "processing"
    row.failure_code = None
    await db.flush()
    emit(db, VoiceSessionReady(
        tenant_id=row.tenant_id, voice_session_id=row.id, candidate_id=row.candidate_id,
        application_id=row.application_id, transcript_id=row.transcript_id, recording_file_id=row.recording_file_id,
    ))  # fmt: skip
    if await start_agent(ctx, db, row, "ready") is None:
        from app.services.voice import agent_ops

        try:
            async with db.begin_nested():
                await agent_ops.extract(ctx, db, row, None)
        except Exception:
            logger.info("Voice answers not extracted for {} (no agent run)", row.id)
        await agent_ops.hand_to_reviewer(ctx, db, row)


async def cancel_active_run(db: AsyncSession, row: VoiceSession, *, reason: str) -> None:
    from app.services import agent_runtime_service

    if row.agent_run_id is None:
        return
    try:
        async with db.begin_nested():
            await agent_runtime_service.cancel_run(
                RequestContext(tenant_id=row.tenant_id, actor_kind="system"), db, row.agent_run_id, reason=reason,
                reason_code="subject_closed",
            )  # fmt: skip
    except Exception:
        logger.debug("Voice screening run {} not cancelled", row.agent_run_id)


# ================================================================ reads / actions
async def list_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> list[VoiceSessionOut]:
    await readable_application(ctx, db, application_id)
    rows = (
        (
            await db.execute(
                select(VoiceSession)
                .where(VoiceSession.application_id == application_id)
                .order_by(VoiceSession.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await common.out(db, r) for r in rows]


async def get(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID) -> VoiceSessionOut:
    return await common.out(db, await readable(ctx, db, session_id))


async def cancel(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID) -> VoiceSessionOut:
    row = await readable(ctx, db, session_id, for_update=True)
    if row.status in CLOSED or row.status == "ready_for_review":
        raise common.conflict(
            "voice_session_closed", "This voice screen can no longer be cancelled", status=row.status
        )
    row.status = "cancelled"
    await db.flush()
    await cancel_active_run(db, row, reason="Cancelled by the recruiter")
    from app.core import secure_links

    await secure_links.revoke_for_subject(db, common.LINK_SUBJECT, row.id, common.LINK_PURPOSE)
    audit.record("voice_session.cancelled", "voice_session", row.id, session=db)
    await common.timeline(ctx, db, row, "Voice screen cancelled", "cancelled")
    return await common.out(db, row)


async def transcript(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID) -> VoiceTranscriptOut:
    from app.services import transcripts_service

    row = await readable(ctx, db, session_id)
    if row.transcript_id is None:
        raise NotFound("No transcript yet")
    t = await transcripts_service.get(
        common.reader_ctx(row.tenant_id, row.owner_user_id, ctx), db, row.transcript_id
    )
    return VoiceTranscriptOut(
        transcript_id=t.id, status=t.status,
        segments=[{"seq": s.seq, "speaker_role": s.speaker_role, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text} for s in t.segments],
    )  # fmt: skip


async def label_speakers(
    ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID, seqs: list[int]
) -> VoiceSessionOut:
    """Mark transcript lines as the Candidate's (uploaded transcripts often carry generic speaker names). Lines that
    were ``unknown`` and not chosen become ``recruiter``. Then the answers are extracted."""
    from app.models.transcripts import TranscriptSegment

    require_human(actor_from_ctx(ctx))
    row = await readable(ctx, db, session_id, for_update=True)
    if row.status in CLOSED:
        raise common.conflict("voice_session_closed", "This voice screen is closed", status=row.status)
    if row.transcript_id is None:
        raise common.conflict("transcript_missing", "Upload a transcript first")
    wanted = sorted(set(seqs))
    await db.execute(
        update(TranscriptSegment)
        .where(TranscriptSegment.transcript_id == row.transcript_id, TranscriptSegment.seq.in_(wanted))
        .values(speaker_role="candidate")
    )
    await db.execute(
        update(TranscriptSegment)
        .where(
            TranscriptSegment.transcript_id == row.transcript_id, TranscriptSegment.seq.not_in(wanted),
            TranscriptSegment.speaker_role.in_(("unknown", "candidate")),
        )
        .values(speaker_role="recruiter")
    )  # fmt: skip
    await db.flush()
    audit.record(
        "voice_session.speakers_labelled", "voice_session", row.id, session=db, candidate_lines=len(wanted)
    )
    if row.status in ("uploaded", "processing", "ready_for_review"):
        await mark_ready(ctx, db, row)
    return await common.out(db, row)


async def complete_review(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID) -> VoiceSessionOut:
    from app.services import screening_agent_service

    require_human(actor_from_ctx(ctx))
    row = await readable(ctx, db, session_id, for_update=True)
    if row.status != "ready_for_review":
        raise common.conflict(
            "voice_session_not_ready", "This voice screen is not ready for review", status=row.status
        )
    try:
        review = await screening_agent_service.complete_review(ctx, db, voice_session_id=row.id)
        row.screening_id = review.screening_id
    except NotFound:
        row.screening_id = (
            None  # no answers were extracted: the recruiter records the outcome on the application
        )
    row.status = "reviewed"
    row.reviewed_by = common.user_id_of(ctx)
    row.reviewed_at = common.now()
    await db.flush()
    audit.record("voice_session.reviewed", "voice_session", row.id, session=db)
    await common.timeline(ctx, db, row, "Voice screen reviewed", "reviewed")
    return await common.out(db, row)


__all__ = [
    "after_consent",
    "cancel",
    "cancel_active_run",
    "complete_review",
    "get",
    "label_speakers",
    "list_for_application",
    "mark_ready",
    "readable",
    "readable_application",
    "request",
    "start_agent",
    "transcript",
]
