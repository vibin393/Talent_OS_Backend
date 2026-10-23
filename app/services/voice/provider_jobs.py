# ruff: file-ignore[line-too-long]
"""Provider jobs (Epic 58, story 58.1; FR-293): the ``voice`` port with the Fake and Manual adapters only.

* ``schedule`` -- ``schedule_call(CallSessionRequestDTO(record=recording_allowed, script_ref, ...))``. Manual raises
  ``ManualActionRequired``: the session stays ``planned`` and the owner gets the Task "Call {candidate} and upload the
  transcript". Fake returns an ``external_ref`` (``scheduled``) and its deterministic lifecycle is played through
  ``handle_event`` (task ``voice.run_fake_call`` on ``connectors``; the HMAC webhook handler feeds the same function).
* ``handle_event`` -- ``started`` -> ``in_progress``; ``ended`` -> the recording (only if ``recording_allowed``, stored in
  tenant files as ``voice_session`` evidence) and the speaker-labelled transcript (``source_kind="voice_session"``) ->
  ``processing`` + ``VoiceSessionReady``; ``no_answer`` / ``failed`` -> ``failed`` + Task.
No real call is ever placed and no audio is analysed.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.models.voice import VoiceSession
from app.services.voice import common

MAX_DURATION_S = 1800


def adapter(provider_key: str) -> Any:
    from app.services.connectors.registry import adapter_factory

    factory = adapter_factory("voice", provider_key)
    if factory is None:
        raise common.invalid(
            "provider_not_available", "This call method is not available", provider_key=provider_key
        )
    return factory(None)


async def _phone(db: AsyncSession, candidate_id: uuid.UUID) -> str:
    from app.models.candidates import CandidatePhone

    value = (
        await db.execute(
            select(CandidatePhone.phone_e164)
            .where(CandidatePhone.candidate_id == candidate_id)
            .order_by(CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return value or ""


async def schedule(
    ctx: RequestContext, db: AsyncSession, row: VoiceSession, at: Any = None  # ruff: ignore[unused-function-argument]
) -> dict[str, Any]:
    from app.services.connectors.base import ManualActionRequired
    from app.services.connectors.voice.port import CallSessionRequestDTO

    if row.status not in ("planned", "consent_pending", "scheduled"):
        raise common.conflict(
            "voice_session_closed", "This voice screen cannot be scheduled", status=row.status
        )
    if not await common.has_consent(db, row.candidate_id, common.PURPOSE_CALL):
        raise common.conflict("voice_consent_missing", "The candidate has not agreed to a screening call")
    provider = adapter(row.provider_key)
    req = CallSessionRequestDTO(
        tenant_id=row.tenant_id, voice_session_id=row.id, to_e164=await _phone(db, row.candidate_id), scheduled_at=at,
        record=bool(row.recording_allowed), language=None, max_duration_s=MAX_DURATION_S, callback_url=None,
        script_ref=row.script_ref,
    )  # fmt: skip
    try:
        dto = await provider.schedule_call(req)
    except ManualActionRequired:
        row.status = "planned"
        await db.flush()
        name = await common.candidate_name(db, row.candidate_id)
        await common.owner_task(
            db, row, f"Call {name} and upload the transcript",
            "Call the candidate outside Talent OS, then upload the transcript (and the recording, if they agreed) on the application's Voice screening panel.",
        )  # fmt: skip
        audit.record("voice_session.manual_call", "voice_session", row.id, session=db)
        return {"status": row.status, "external_ref": None, "manual_required": True}
    row.external_ref = dto.external_ref
    row.status = "scheduled"
    row.scheduled_at = at or common.now()
    await db.flush()
    audit.record(
        "voice_session.scheduled", "voice_session", row.id, session=db, provider_key=row.provider_key
    )
    if row.provider_key == "fake":  # after commit: the lifecycle runs in its own transaction
        from app.core.events import emit

        emit(db, common.FakeCallScheduled(tenant_id=row.tenant_id, voice_session_id=row.id))
    return {"status": row.status, "external_ref": row.external_ref, "manual_required": False}


async def run_fake_call(db: AsyncSession, session_id: uuid.UUID) -> int:
    """Play the Fake adapter's deterministic lifecycle for a scheduled session (tests / demos)."""
    row = await common.session_row(db, session_id, for_update=True)
    if row.provider_key != "fake" or row.external_ref is None or row.status != "scheduled":
        return 0
    provider = adapter("fake")
    n = 0
    for event in provider.lifecycle(row.external_ref, record=bool(row.recording_allowed)):
        await handle_event(db, row, event.kind)
        n += 1
    return n


async def _store_recording(ctx: RequestContext, db: AsyncSession, row: VoiceSession, rec: Any) -> None:
    from app.services import files_service

    if not row.recording_allowed:
        return  # the gate never allowed it: nothing is stored
    stored = await files_service.store_generated(
        ctx, db, rec.data, key=storage.new_key(row.tenant_id), filename=f"{row.human_id}.wav", content_type=rec.media_type,
        owner_entity_type=common.FILE_OWNER, owner_entity_id=row.id,
    )  # fmt: skip
    row.recording_file_id = stored.id
    row.recording_media_type = rec.media_type
    row.duration_s = rec.duration_s


async def handle_event(db: AsyncSession, row: VoiceSession, kind: str) -> None:
    from app.schemas.transcripts import TranscriptSegmentIn
    from app.services import transcripts_service
    from app.services.voice import sessions

    ctx = common.reader_ctx(row.tenant_id, row.owner_user_id)
    if row.status in ("reviewed", "cancelled", "failed"):
        return
    if kind == "started":
        row.status = "in_progress"
        row.started_at = common.now()
    elif kind == "ended":
        provider = adapter(row.provider_key)
        call = await provider.end_call(row.external_ref or "")
        row.ended_at = common.now()
        if call.recording_ref and row.recording_allowed:
            rec = await provider.fetch_recording(call.recording_ref)
            if rec is not None:
                await _store_recording(ctx, db, row, rec)
        dto = await provider.fetch_transcript(call.transcript_ref) if call.transcript_ref else None
        if dto is None or not dto.segments:
            row.status = "uploaded"
            await db.flush()
            await common.owner_task(
                db, row, f"Upload a transcript for {await common.candidate_name(db, row.candidate_id)}"
            )
            return
        t = await transcripts_service.create(
            ctx, db, source_kind="voice_session", source_id=row.id, candidate_id=row.candidate_id,
            application_id=row.application_id, language=dto.language,
        )  # fmt: skip
        segs = [
            TranscriptSegmentIn(speaker_role=s.speaker, start_ms=s.start_ms, end_ms=s.end_ms, text=s.text)
            for s in dto.segments
        ]
        await transcripts_service.append_segments(ctx, db, t.id, segs, final=True)
        row.transcript_id = t.id
        row.transcript_source = "provider"
        await db.flush()
        await sessions.mark_ready(ctx, db, row)
        return
    elif kind in ("no_answer", "failed"):
        row.status = "failed"
        row.failure_code = kind
        await db.flush()
        name = await common.candidate_name(db, row.candidate_id)
        await common.owner_task(
            db, row, f"Voice screening call with {name} did not happen ({kind.replace('_', ' ')})"
        )
    await db.flush()


async def on_webhook(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any], config: Any) -> None:
    """``connectors.process_inbound`` handler for the ``voice`` category: ``{external_ref, kind}``. Epic 76: the
    registered interceptors (telephony) see the payload first."""
    if await _intercepted(ctx, db, payload, config):
        return
    ref = str(payload.get("external_ref") or "")
    kind = str(payload.get("kind") or "")
    if not ref or kind not in ("started", "ended", "no_answer", "failed"):
        return
    row = (
        await db.execute(select(VoiceSession).where(VoiceSession.external_ref == ref).with_for_update())
    ).scalar_one_or_none()
    if row is not None:
        await handle_event(db, row, kind)


# ================================================================ Epic 76 additive hooks (D-22; P5-free)
# Phase 5 telephony drives real bridged calls from its own package (``services/telephony``); P4 never imports it.
# * ``register_event_interceptor`` -- the ``voice`` webhook handler first offers each inbound payload to the registered
#   interceptors (telephony's ``voice_calls`` lifecycle); a truthy return means "handled".
# * ``on_provider_event`` -- advance a session from a provider event without re-fetching (telephony stores the
#   recording and runs speech-to-text itself).
EventInterceptor = Callable[[RequestContext, AsyncSession, Mapping[str, Any], Any], Awaitable[bool]]
_interceptors: list[EventInterceptor] = []


def register_event_interceptor(fn: EventInterceptor) -> None:
    if fn not in _interceptors:
        _interceptors.append(fn)


async def _intercepted(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any], config: Any) -> bool:
    for fn in list(_interceptors):
        if await fn(ctx, db, payload, config):
            return True
    return False


async def on_provider_event(
    ctx: RequestContext,
    db: AsyncSession,
    session_id: uuid.UUID,
    kind: str,
    *,
    provider_key: str | None = None,
    duration_s: int | None = None,
    awaiting_recording: bool = False,
    recording_file_id: uuid.UUID | None = None,
    recording_media_type: str | None = None,
    transcript_id: uuid.UUID | None = None,
) -> str:
    """Advance a session from a telephony event. ``kind``: ``call_placed`` (-> ``scheduled``, mode ``call``),
    ``started`` (-> ``in_progress``), ``not_connected`` (back to ``planned``: the recruiter may retry), ``ended``
    (``uploaded`` + owner Task unless a recording is awaited), ``recording_stored`` (the gate-approved recording file),
    ``transcript_ready`` (provider transcript -> ``mark_ready``), ``transcribe_failed`` (``uploaded`` + Task +
    ``voice_upload_needed``), ``failed`` / ``no_answer`` (as ``handle_event``). Returns the session status."""
    from app.services.voice import sessions

    row = await common.session_row(db, session_id, for_update=True)
    if row.status in ("reviewed", "cancelled", "failed"):
        return row.status
    now = common.now()
    if kind == "call_placed":
        row.mode = "call"
        if provider_key:
            row.provider_key = provider_key
        if row.status in ("planned", "consent_pending", "uploaded", "scheduled"):
            row.status = "scheduled"
            row.scheduled_at = now
            row.failure_code = None
    elif kind == "started":
        if row.status in ("planned", "scheduled", "uploaded"):
            row.status = "in_progress"
            row.started_at = row.started_at or now
    elif kind == "not_connected":
        if row.status in ("scheduled", "in_progress"):
            row.status = "planned"
    elif kind == "ended":
        row.ended_at = now
        if duration_s is not None:
            row.duration_s = duration_s
        if not awaiting_recording and row.transcript_id is None and row.status in ("scheduled", "in_progress"):
            row.status = "uploaded"
            await db.flush()
            name = await common.candidate_name(db, row.candidate_id)
            await common.owner_task(
                db, row, f"Upload a transcript for {name}",
                "The call was not recorded. Upload the call notes or transcript so the assistant can extract answers.",
            )  # fmt: skip
    elif kind == "recording_stored" and recording_file_id is not None:
        row.recording_allowed = True
        row.recording_file_id = recording_file_id
        row.recording_media_type = recording_media_type or "audio/wav"
        row.recording_consent_id = await common.active_consent_id(db, row.candidate_id, common.PURPOSE_RECORDING)
        row.recording_deleted_at = None
        if row.status in ("scheduled", "in_progress"):
            row.status = "uploaded"
    elif kind == "transcript_ready" and transcript_id is not None:
        row.transcript_id = transcript_id
        row.transcript_source = "provider"
        await db.flush()
        await sessions.mark_ready(ctx, db, row)
        return row.status
    elif kind == "transcribe_failed":
        if row.transcript_id is None and row.status not in ("ready_for_review",):
            row.status = "uploaded"
            await db.flush()
            name = await common.candidate_name(db, row.candidate_id)
            await common.owner_task(
                db, row, f"Upload a transcript or enter answers for {name}",
                "Automatic transcription did not work for this call. Upload the transcript or enter the answers.",
            )  # fmt: skip
            common.fan_out(row.tenant_id, "voice_upload_needed", {
                "event_id": f"voice_upload_needed:{row.id}", "voice_session_id": str(row.id), "user_id": str(row.owner_user_id),
                "application_id": str(row.application_id) if row.application_id else None,
            })  # fmt: skip
    elif kind in ("failed", "no_answer"):
        await handle_event(db, row, kind)
        return row.status
    await db.flush()
    return row.status


__all__ = [
    "adapter",
    "handle_event",
    "on_provider_event",
    "on_webhook",
    "register_event_interceptor",
    "run_fake_call",
    "schedule",
]
