# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Manual uploads (Epic 58, stories 58.1 / 58.3; FR-293, FR-294).

* **Transcript** (TXT, VTT, SRT, DOCX; <= 5 MB): needs ``voice_screening`` consent (409 ``voice_consent_missing``) and
  the attestation; stored scanned in tenant files, then ``transcripts_service.ingest_upload(source_kind="voice_session")``.
  Unlabelled lines are ``unknown``: without a ``candidate`` line the recruiter marks the candidate's lines first.
* **Recording** (MP3, M4A, WAV, OGG; <= ``max_upload_mb`` <= 200 MB; sniffed, video refused 422 ``unsupported_media``,
  malware-scanned): the Recording Policy gate is re-run now; not allowed -> 409 ``recording_not_permitted`` with the
  gate's ``reasons``. ``recording_allowed`` / ``recording_file_id`` are written only from an allowing decision. Without a
  transcript the session is ``uploaded`` and the owner is asked for one (no speech-to-text in Phase 4).
* Every upload stores an attestation (who, when, statement version, consent obtained, policy followed, gate snapshot).
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.models.voice import VoiceSession, VoiceUploadAttestation
from app.schemas.voice import VoiceSessionOut
from app.services.voice import common, sessions

TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024
CLOSED = ("reviewed", "cancelled", "failed")

_transcriber: Any | None = None


def set_transcription_provider(provider: Any | None) -> None:
    """Register a ``TranscriptionProvider`` (the Fake in tests / demos). Phase 4 ships none (no STT)."""
    global _transcriber
    _transcriber = provider


def transcription_provider() -> Any | None:
    return _transcriber


# Epic 76 (A7, additive; D-22): resolvers registered by Phase 5 telephony (P4 never imports it). A resolver returns a
# provider or None; a provider with ``enqueue_for_session(ctx, db, row)`` queues speech-to-text instead of
# transcribing inline (the session stays ``uploaded`` and no manual Task is created).
TranscriptionResolver = Callable[[RequestContext, AsyncSession], Awaitable[Any | None]]
_resolvers: list[TranscriptionResolver] = []


def register_transcription_resolver(fn: TranscriptionResolver) -> None:
    if fn not in _resolvers:
        _resolvers.append(fn)


async def resolve_transcription_provider(ctx: RequestContext, db: AsyncSession) -> Any | None:
    """The module provider (tests / demos) wins; else the first registered resolver's provider."""
    if _transcriber is not None:
        return _transcriber
    for fn in list(_resolvers):
        found = await fn(ctx, db)
        if found is not None:
            return found
    return None


def _attest(consent_obtained: bool, policy_followed: bool) -> None:
    if not (consent_obtained and policy_followed):
        raise common.invalid(
            "attestation_required", "Confirm the candidate agreed and that you followed the recording policy",
            fields={"consent_obtained": "Required", "policy_followed": "Required"},
        )  # fmt: skip


async def _session(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID) -> VoiceSession:
    row = await sessions.readable(ctx, db, session_id, for_update=True)
    if row.status in CLOSED:
        raise common.conflict("voice_session_closed", "This voice screen is closed", status=row.status)
    return row


async def _gate(db: AsyncSession, row: VoiceSession, uploader: uuid.UUID | None = None) -> Any:
    """Known gap 4: under ``all_party_consent`` the uploader's attestation is recorded as their acknowledgement."""
    from app.services.recording_policy import gate

    rctx = common.reader_ctx(row.tenant_id, row.owner_user_id)
    confirmed = (row.confirmed_country, row.confirmed_region) if row.confirmed_country else None
    return await gate.check(
        rctx,
        db,
        purpose=common.PURPOSE_RECORDING,
        candidate_id=row.candidate_id,
        participant_user_ids=[uploader] if uploader else [],
        acks={uploader: common.now()} if uploader else {},
        confirmed_jurisdiction=confirmed,
    )  # type: ignore[arg-type]


def _snapshot(decision: Any) -> dict[str, Any]:
    data = dataclasses.asdict(decision)
    return {k: list(v) if isinstance(v, tuple) else v for k, v in data.items()}


def _record(
    db: AsyncSession, row: VoiceSession, kind: str, file_id: uuid.UUID, user_id: uuid.UUID, decision: Any
) -> None:
    db.add(
        VoiceUploadAttestation(
            id=uuid.uuid4(), tenant_id=row.tenant_id, voice_session_id=row.id, kind=kind, file_id=file_id,
            statement_version=common.STATEMENT_VERSION, consent_obtained=True, policy_followed=True, attested_by=user_id,
            attested_at=common.now(), gate_snapshot=_snapshot(decision),
        )
    )  # fmt: skip
    row.attested_by = user_id
    row.attested_at = common.now()


def _uploader(ctx: RequestContext) -> uuid.UUID:
    uid = common.user_id_of(ctx)
    if uid is None:
        raise common.conflict("human_only_action", "Only a person can upload call evidence")
    return uid


async def _replace_transcript(ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> None:
    """A new transcript before review supersedes the previous one (erased) and its voice answers."""
    from app.models.transcripts import Transcript
    from app.services import screening_agent_service
    from app.services.transcripts.service import erase_rows

    if row.transcript_id is None:
        return
    old = await db.get(Transcript, row.transcript_id)
    row.transcript_id = None
    await db.flush()
    if old is not None:
        await erase_rows(db, [old], reason="voice_transcript_replaced")
    await screening_agent_service.erase_voice_answers(ctx, db, row.id)


async def upload_transcript(
    ctx: RequestContext,
    db: AsyncSession,
    session_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    *,
    consent_obtained: bool,
    policy_followed: bool,
) -> VoiceSessionOut:
    from app.services import files_service, transcripts_service

    row = await _session(ctx, db, session_id)
    if row.status in CLOSED:
        raise common.conflict("voice_session_closed", "This voice screen is closed", status=row.status)
    if not await common.has_consent(db, row.candidate_id, common.PURPOSE_CALL):
        raise common.conflict("voice_consent_missing", "The candidate has not agreed to the screening call")
    _attest(consent_obtained, policy_followed)
    uid = _uploader(ctx)
    if len(data) > TRANSCRIPT_MAX_BYTES:
        raise common.TooLarge("Transcript files are limited to 5 MB", details={"max_mb": 5})
    if not data.strip():
        raise common.invalid("transcript_unparseable", "The transcript file is empty")
    content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if data.startswith(b"PK\x03\x04") else "text/plain"
    )  # fmt: skip
    stored = await files_service.store_generated(
        ctx, db, data, key=storage.new_key(row.tenant_id), filename=filename or "transcript.txt",
        content_type=content_type, owner_entity_type=common.FILE_OWNER, owner_entity_id=row.id,
    )  # fmt: skip
    await _replace_transcript(ctx, db, row)
    t = await transcripts_service.ingest_upload(
        common.reader_ctx(row.tenant_id, row.owner_user_id, ctx), db, file_id=stored.id, source_kind="voice_session",
        source_id=row.id, candidate_id=row.candidate_id, application_id=row.application_id, attested_by=uid,
    )  # fmt: skip
    _record(db, row, "transcript", stored.id, uid, await _gate(db, row))
    row.transcript_id = t.id
    row.transcript_source = "upload"
    row.status = "uploaded"
    row.voice_consent_id = row.voice_consent_id or await common.active_consent_id(
        db, row.candidate_id, common.PURPOSE_CALL
    )
    await db.flush()
    audit.record(
        "voice_session.transcript_uploaded", "voice_session", row.id, session=db, segments=t.segment_count
    )
    await common.timeline(ctx, db, row, "Voice screen transcript uploaded", "transcript_uploaded")
    if await common.needs_speaker_label(db, row):
        await common.owner_task(
            db,
            row,
            f"Mark the candidate's lines in the transcript for {await common.candidate_name(db, row.candidate_id)}",
        )
    else:
        await sessions.mark_ready(ctx, db, row)
    return await common.out(db, row)


def audio_kind(data: bytes, content_type: str | None) -> str:
    from app.services import files_service

    if (content_type or "").lower().startswith("video/"):
        raise common.invalid("unsupported_media", "Video files aren't accepted. Upload an audio recording.")
    kind = files_service.sniff_audio(data)
    if kind is None:
        raise common.invalid("unsupported_media", "Only audio recordings (MP3, M4A, WAV, OGG) are accepted.")
    return kind


async def upload_recording(
    ctx: RequestContext,
    db: AsyncSession,
    session_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    content_type: str | None,
    *,
    consent_obtained: bool,
    policy_followed: bool,
) -> VoiceSessionOut:
    from app.services import files_service

    row = await _session(ctx, db, session_id)
    if row.status in CLOSED:
        raise common.conflict("voice_session_closed", "This voice screen is closed", status=row.status)
    limit = int((await common.settings_row(db)).max_upload_mb)
    if len(data) > limit * 1024 * 1024:
        raise common.TooLarge(f"Recordings are limited to {limit} MB", details={"max_mb": limit})
    kind = audio_kind(data, content_type)
    decision = await _gate(db, row, common.user_id_of(ctx))
    if not decision.recording_allowed:
        row.jurisdiction = decision.jurisdiction
        row.recording_rule = decision.rule
        await db.flush()
        raise common.conflict(
            "recording_not_permitted", "Recording isn't permitted for this candidate", reasons=list(decision.reasons),
            rule=decision.rule, jurisdiction=decision.jurisdiction,
        )  # fmt: skip
    _attest(consent_obtained, policy_followed)
    uid = _uploader(ctx)
    stored = await files_service.store_upload(ctx, db, data, filename, {kind}, common.FILE_OWNER, row.id)
    if row.recording_file_id is not None:
        await files_service.purge_file(db, row.recording_file_id)
    row.jurisdiction = decision.jurisdiction
    row.recording_rule = decision.rule
    row.recording_allowed = True
    row.recording_file_id = stored.id
    row.recording_media_type = files_service.KIND_CONTENT_TYPES[kind]
    row.recording_consent_id = await common.active_consent_id(db, row.candidate_id, common.PURPOSE_RECORDING)
    row.recording_deleted_at = None
    _record(db, row, "recording", stored.id, uid, decision)
    await db.flush()
    audit.record(
        "voice_session.recording_uploaded",
        "voice_session",
        row.id,
        session=db,
        media_type=row.recording_media_type,
    )
    await common.timeline(ctx, db, row, "Voice screen recording uploaded", "recording_uploaded")
    if row.transcript_id is None:
        provider = await resolve_transcription_provider(ctx, db)
        if provider is not None and hasattr(provider, "enqueue_for_session"):
            await provider.enqueue_for_session(ctx, db, row)
            row.status = "uploaded"  # transcription queued (no manual Task)
            await db.flush()
        elif provider is not None:
            await transcribe(ctx, db, row)
        else:
            row.status = "uploaded"
            await db.flush()
            name = await common.candidate_name(db, row.candidate_id)
            await common.owner_task(
                db,
                row,
                f"Upload a transcript for {name}",
                "Talent OS does not transcribe audio. Upload the call transcript so the assistant can extract answers.",
            )
            common.fan_out(row.tenant_id, "voice_upload_needed", {
                "event_id": f"voice_upload_needed:{row.id}", "voice_session_id": str(row.id), "user_id": str(row.owner_user_id),
                "application_id": str(row.application_id) if row.application_id else None,
            })  # fmt: skip
    return await common.out(db, row)


async def transcribe(ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> uuid.UUID:
    """Only with a registered ``TranscriptionProvider`` (the Fake in Phase 4), else ``not_available``."""
    from app.schemas.transcripts import TranscriptSegmentIn
    from app.services import files_service, transcripts_service

    provider = transcription_provider()
    if provider is None:
        raise common.invalid("not_available", "No transcription service is available. Upload a transcript.")
    if row.recording_file_id is None:
        raise common.conflict("recording_missing", "There is no recording to transcribe")
    if row.transcript_id is not None:
        return row.transcript_id
    file = await files_service.get_file(db, row.recording_file_id)
    audio = await files_service.read_file(file)  # type: ignore[arg-type]
    dto = await provider.transcribe(audio, row.recording_media_type or "audio/wav", None)
    rctx = common.reader_ctx(row.tenant_id, row.owner_user_id, ctx)
    t = await transcripts_service.create(
        rctx, db, source_kind="voice_session", source_id=row.id, candidate_id=row.candidate_id,
        application_id=row.application_id, language=dto.language,
    )  # fmt: skip
    segs = [
        TranscriptSegmentIn(speaker_role=s.speaker, start_ms=s.start_ms, end_ms=s.end_ms, text=s.text)
        for s in dto.segments
    ]
    await transcripts_service.append_segments(rctx, db, t.id, segs, final=True)
    row.transcript_id = t.id
    row.transcript_source = "provider"
    await db.flush()
    await sessions.mark_ready(ctx, db, row)
    return t.id


async def delete_attestations(db: AsyncSession, session_ids: list[uuid.UUID]) -> int:
    if not session_ids:
        return 0
    result = await db.execute(
        delete(VoiceUploadAttestation).where(VoiceUploadAttestation.voice_session_id.in_(session_ids))
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def attestations(db: AsyncSession, session_id: uuid.UUID) -> list[VoiceUploadAttestation]:
    return list(
        (
            await db.execute(
                select(VoiceUploadAttestation).where(VoiceUploadAttestation.voice_session_id == session_id)
            )
        ).scalars()
    )


__all__ = [
    "attestations",
    "audio_kind",
    "delete_attestations",
    "set_transcription_provider",
    "transcribe",
    "transcription_provider",
    "upload_recording",
    "upload_transcript",
]
