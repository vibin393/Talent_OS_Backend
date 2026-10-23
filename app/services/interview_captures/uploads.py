# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Uploads (Epic 60, story 60.2; FR-302): a meeting transcript export and / or an audio recording.

* **Transcript** (TXT, VTT, SRT, DOCX -- Teams, Zoom or Meet exports; at most 5 MB): stored scanned in tenant storage,
  then ``transcripts_service.ingest_upload(source_kind="interview_capture")``. Speaker names are mapped to the
  Interview's participants (internal interviewers by name -> ``interviewer``; the Candidate by name -> ``candidate``);
  unmatched names stay ``unknown`` and the capture waits for "Who is the candidate?" before processing.
* **Recording** (MP3, M4A, WAV, OGG -- audio only; at most the tenant's ``max_upload_mb`` <= 200 MB): sniffed from the
  bytes; ``video/*`` and containers with a video track -> 422 ``unsupported_media``. Stored scanned as evidence
  (``file_id``, ``recording_allowed``). There is no speech-to-text in Phase 4 (coordinator decision 2): without a
  transcript the capture is ``uploaded`` and the uploader is asked for the meeting transcript. A registered
  ``TranscriptionProvider`` (the Fake, tests / demos only) produces one.
* Every upload re-checks the gate (all participants consented, policy permits) and needs the attestation "This is the
  interview everyone agreed to record". Only the words are processed: no audio or video feature is ever analysed.
* ``TranscriptReady(source_kind="interview_capture")`` -> status ``processing`` and ``InterviewCaptureReady`` (the
  ``capture_ready`` agent trigger), unless speakers still need mapping.
"""

import dataclasses
import re
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.core.events import emit
from app.core.permissions import Principal, Scope
from app.events.interview_captures import InterviewCaptureReady
from app.models.interview_captures import InterviewCapture, InterviewCaptureOutput, InterviewScorecardDraft
from app.models.interviews import Interview
from app.models.transcripts import TranscriptSegment
from app.services.interview_captures import common, consent
from app.services.interview_captures.common import now

TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024
TRANSCRIPT_EXTENSIONS = (".txt", ".vtt", ".srt", ".docx")
AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg"}
_LABEL = re.compile(r"^([A-Za-z][A-Za-z0-9 ._'-]{0,39}?)\s*:\s+(\S.*)$", re.DOTALL)

_transcriber: Any | None = None


def set_transcription_provider(provider: Any | None) -> None:
    """Register a ``TranscriptionProvider`` (the Fake in tests / demos). Phase 4 ships none (no STT)."""
    global _transcriber
    _transcriber = provider


def transcription_provider() -> Any | None:
    return _transcriber


def reader_ctx(ctx: RequestContext) -> RequestContext:
    """A context for the transcripts service's Candidate visibility check once the caller's Interview access (participant
    with ``interview_capture:manage``) has been established; the actor is unchanged."""
    principal = ctx.principal
    if not isinstance(principal, Principal):
        return ctx
    perms = dict(principal.permissions or {})
    perms["candidate:read"] = Scope.TENANT
    kind = "user" if principal.type == "agent" else principal.type
    pid = ctx.on_behalf_of if principal.type == "agent" and ctx.on_behalf_of else principal.id
    return dataclasses.replace(
        ctx, principal=dataclasses.replace(principal, type=kind, id=pid, permissions=perms)
    )


# ================================================================ sniffing
def audio_kind(data: bytes, content_type: str | None) -> str:
    """``mp3 | m4a | wav | ogg``; video (declared or detected) and anything else -> 422 ``unsupported_media``."""
    declared = (content_type or "").lower()
    if declared.startswith("video/"):
        raise common.invalid("unsupported_media", "Video files aren't accepted. Only the words are used.")
    head = data[:64]
    kind: str | None = None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        kind = "wav"
    elif head[:4] == b"OggS":
        kind = "ogg"
        if b"theora" in data[:65536] or b"\x01video" in data[:65536]:
            kind = None
    elif head[4:8] == b"ftyp":
        brand = head[8:12]
        kind = "m4a" if brand in (b"M4A ", b"M4B ", b"mp42", b"isom", b"dash", b"mp41") else None
        if kind and (b"vide" in data[: 4 * 1024 * 1024] or brand in (b"qt  ",)):
            kind = None
    elif head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        kind = "mp3"
    if kind is None:
        raise common.invalid(
            "unsupported_media",
            "Only audio recordings (MP3, M4A, WAV, OGG) are accepted. Video files aren't accepted.",
        )
    return kind


def transcript_content_type(data: bytes) -> str:
    if data.startswith(b"PK\x03\x04"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "text/plain"


# ================================================================ helpers
async def _upload_capture(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, attest: bool
) -> tuple[Interview, InterviewCapture]:
    interview = await consent.manageable_interview(ctx, db, interview_id)
    principal = common.principal_of(ctx)
    if not await consent.is_participant(db, interview, principal.id):
        raise common.conflict(
            "not_a_participant", "Only a participant of the interview can upload its capture"
        )
    if not attest:
        raise common.invalid(
            "attestation_required", "Confirm this is the interview everyone agreed to record",
            fields={"attest": "Required"},
        )  # fmt: skip
    capture = await common.capture_for_interview(db, interview_id, for_update=True)
    if capture is None or capture.status == "erased":
        raise common.conflict("capture_not_requested", "Ask for consent before uploading")
    ev = await consent.refresh(db, interview, capture)
    if not ev.permitted:
        raise common.conflict(
            "capture_not_permitted", "Recording isn't available: not everyone has agreed or the policy doesn't allow it",
            reasons=list(ev.reasons),
        )  # fmt: skip
    return interview, capture


async def _store(
    ctx: RequestContext,
    db: AsyncSession,
    data: bytes,
    filename: str,
    content_type: str,
    capture: InterviewCapture,
) -> Any:
    from app.services import files_service

    return await files_service.store_generated(
        ctx, db, data, key=storage.new_key(capture.tenant_id), filename=filename, content_type=content_type,
        owner_entity_type="interview", owner_entity_id=capture.interview_id,
    )  # fmt: skip


async def supersede_outputs(db: AsyncSession, capture_id: uuid.UUID) -> None:
    await db.execute(
        update(InterviewCaptureOutput)
        .where(InterviewCaptureOutput.capture_id == capture_id, InterviewCaptureOutput.status == "ready")
        .values(status="superseded")
    )
    await db.execute(
        update(InterviewScorecardDraft)
        .where(
            InterviewScorecardDraft.capture_id == capture_id,
            InterviewScorecardDraft.status.in_(("ready", "opened", "dismissed")),
        )
        .values(status="superseded")
    )


def _norm_name(name: str | None) -> str:
    return " ".join((name or "").lower().split())


async def _name_map(db: AsyncSession, interview: Interview) -> dict[str, tuple[str, uuid.UUID | None]]:
    """Normalised speaker name (full and first name) -> (role, ref)."""
    from app.services import entity_refs

    out: dict[str, tuple[str, uuid.UUID | None]] = {}
    parts = await common.participants(db, interview.id)
    users = await entity_refs.users(db, [p.user_id for p in parts if p.user_id])
    for p in parts:
        if p.kind == "candidate":
            continue
        names = {p.name, users[p.user_id].name if p.user_id in users else None}
        for n in names:
            key = _norm_name(n)
            if key:
                out.setdefault(key, ("interviewer", p.user_id))
                out.setdefault(key.split()[0], ("interviewer", p.user_id))
    cand = await entity_refs.addressee(db, "candidate", interview.candidate_id)
    key = _norm_name(cand.full_name if cand else None)
    if key:
        out[key] = ("candidate", None)
        out.setdefault(key.split()[0], ("candidate", None))
    return out


async def map_speakers(
    db: AsyncSession,
    interview: Interview,
    capture: InterviewCapture,
    transcript_id: uuid.UUID,
    *,
    candidate_speaker: str | None = None,
) -> list[str]:
    """Relabel ``unknown`` segments whose text starts with ``Name:`` from the participants (and, after "Who is the
    candidate?", the chosen name -> candidate, every other named speaker -> interviewer). Returns unmatched names."""
    names = await _name_map(db, interview)
    chosen = _norm_name(candidate_speaker)
    segments = list(
        (
            await db.execute(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.transcript_id == transcript_id,
                    TranscriptSegment.speaker_role == "unknown",
                )
                .order_by(TranscriptSegment.seq)
            )
        ).scalars()
    )
    unmatched: list[str] = []
    for seg in segments:
        m = _LABEL.match(seg.text)
        if m is None:
            continue
        raw, body = m.group(1).strip(), m.group(2).strip()
        key = _norm_name(raw)
        role_ref = names.get(key)
        if chosen:
            role_ref = (
                ("candidate", None)
                if key == chosen
                else (role_ref if role_ref and role_ref[0] == "interviewer" else ("interviewer", None))
            )
        if role_ref is None:
            if raw not in unmatched:
                unmatched.append(raw)
            continue
        seg.speaker_role, seg.speaker_ref = role_ref[0], role_ref[1]
        seg.text = body
    capture.speakers = unmatched[:20]
    await db.flush()
    return unmatched


async def _ready(db: AsyncSession, capture: InterviewCapture) -> None:
    """The transcript is ready and speakers are known -> ``processing`` + ``InterviewCaptureReady``."""
    if capture.transcript_id is None or capture.speakers:
        return
    capture.status = "processing"
    capture.failure_code = None
    await db.flush()
    emit(
        db,
        InterviewCaptureReady(
            tenant_id=capture.tenant_id, capture_id=capture.id, interview_id=capture.interview_id,
            candidate_id=capture.candidate_id, transcript_id=capture.transcript_id,
        ),
    )  # fmt: skip


async def _ingest(
    ctx: RequestContext, db: AsyncSession, interview: Interview, capture: InterviewCapture, file_id: uuid.UUID
) -> uuid.UUID:
    from app.services import transcripts_service

    principal = common.principal_of(ctx)
    old = capture.transcript_id
    out = await transcripts_service.ingest_upload(
        reader_ctx(ctx), db, file_id=file_id, source_kind="interview_capture", candidate_id=capture.candidate_id,
        application_id=interview.application_id, interview_id=interview.id, source_id=capture.id,
        attested_by=principal.id if principal.type == "user" else None,
    )  # fmt: skip
    if old is not None and old != out.id:
        await supersede_outputs(db, capture.id)
        await transcripts_service.erase(reader_ctx(ctx), db, old, reason="superseded")
    capture.transcript_id = out.id
    await map_speakers(db, interview, capture, out.id)
    return out.id


# ================================================================ public API
async def upload_transcript(
    ctx: RequestContext,
    db: AsyncSession,
    interview_id: uuid.UUID,
    *,
    filename: str | None,
    data: bytes,
    attest: bool,
) -> dict[str, Any]:
    settings = await common.settings_row(db)
    if not settings.allow_transcript_upload:
        raise common.conflict(
            "transcript_upload_disabled", "Transcript uploads are turned off for your organisation"
        )
    name = (filename or "transcript.txt").strip() or "transcript.txt"
    if not name.lower().endswith(TRANSCRIPT_EXTENSIONS):
        raise common.invalid("unsupported_media", "Upload a meeting transcript (VTT, TXT, SRT or DOCX)")
    if len(data) > TRANSCRIPT_MAX_BYTES:
        raise common.invalid("file_too_large", "Transcript files are limited to 5 MB", max_mb=5)
    if not data:
        raise common.invalid("transcript_unparseable", "The transcript file is empty")
    interview, capture = await _upload_capture(ctx, db, interview_id, attest)
    stored = await _store(ctx, db, data, name, transcript_content_type(data), capture)
    transcript_id = await _ingest(ctx, db, interview, capture, stored.id)
    principal = common.principal_of(ctx)
    capture.uploaded_by, capture.uploaded_at, capture.attested_at = principal.id, now(), now()
    capture.capture_kind = "both" if capture.file_id is not None else "transcript"
    capture.status = "uploaded"
    await db.flush()
    await _ready(db, capture)
    audit.record(
        "interview_capture.transcript_uploaded",
        "interview",
        interview.id,
        session=db,
        capture_id=str(capture.id),
    )
    return {
        "interview": interview,
        "capture": capture,
        "transcript_id": transcript_id,
        "needs_transcript": False,
    }


async def upload_recording(
    ctx: RequestContext,
    db: AsyncSession,
    interview_id: uuid.UUID,
    *,
    filename: str | None,
    data: bytes,
    content_type: str | None,
    attest: bool,
) -> dict[str, Any]:
    settings = await common.settings_row(db)
    if not settings.allow_recording_upload:
        raise common.conflict(
            "recording_upload_disabled", "Recording uploads are turned off for your organisation"
        )
    kind = audio_kind(data, content_type)
    limit = int(settings.max_upload_mb)
    if len(data) > limit * 1024 * 1024:
        raise common.invalid("file_too_large", f"Recordings are limited to {limit} MB", max_mb=limit)
    interview, capture = await _upload_capture(ctx, db, interview_id, attest)
    from app.services import files_service

    old_file = capture.file_id
    stored = await _store(ctx, db, data, filename or f"recording.{kind}", AUDIO_TYPES[kind], capture)
    capture.recording_allowed = True
    capture.file_id = stored.id
    capture.file_media_type = AUDIO_TYPES[kind]
    capture.recording_deleted_at = None
    if old_file is not None and old_file != stored.id:
        await files_service.purge_file(db, old_file)
    principal = common.principal_of(ctx)
    capture.uploaded_by, capture.uploaded_at, capture.attested_at = principal.id, now(), now()
    capture.capture_kind = "both" if capture.transcript_id is not None else "recording"
    if capture.status in common.OPEN_STATUSES:
        capture.status = "uploaded"
    await db.flush()
    transcript_id = capture.transcript_id
    provider = transcription_provider()
    if transcript_id is None and provider is not None:
        transcript_id = await transcribe(ctx, db, capture, data=data)
    audit.record(
        "interview_capture.recording_uploaded",
        "interview",
        interview.id,
        session=db,
        capture_id=str(capture.id),
    )
    return {
        "interview": interview,
        "capture": capture,
        "transcript_id": transcript_id,
        "needs_transcript": transcript_id is None,
    }


async def transcribe(
    ctx: RequestContext, db: AsyncSession, capture: InterviewCapture, *, data: bytes | None = None
) -> uuid.UUID:
    """Transcribe the stored recording with the registered provider (the Fake only; no STT in Phase 4)."""
    from app.schemas.transcripts import TranscriptSegmentIn
    from app.services import files_service, transcripts_service

    provider = transcription_provider()
    if provider is None:
        raise common.conflict(
            "not_available", "Speech-to-text isn't available; upload the meeting transcript"
        )
    if capture.file_id is None:
        raise common.conflict("no_recording", "There is no recording to transcribe")
    if data is None:
        file = await files_service.get_file(db, capture.file_id)
        if file is None or file.deleted_at is not None:
            raise common.conflict("no_recording", "There is no recording to transcribe")
        data = await files_service.read_file(file)
    dto = await provider.transcribe(data, capture.file_media_type or "audio/wav", None)
    interview = await common.interview_row(db, capture.interview_id)
    rctx = reader_ctx(ctx)
    created = await transcripts_service.create(
        rctx, db, source_kind="interview_capture", source_id=capture.id, candidate_id=capture.candidate_id,
        application_id=interview.application_id, interview_id=interview.id, language=dto.language,
    )  # fmt: skip
    segments = [
        TranscriptSegmentIn(
            speaker_role=s.speaker if s.speaker in ("candidate", "interviewer", "unknown") else "unknown",
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            text=s.text,
        )
        for s in dto.segments
    ]
    out = await transcripts_service.append_segments(rctx, db, created.id, segments, final=True)
    if capture.transcript_id is not None and capture.transcript_id != out.id:
        await supersede_outputs(db, capture.id)
    capture.transcript_id = out.id
    capture.capture_kind = "both"
    capture.speakers = []
    await db.flush()
    await _ready(db, capture)
    return out.id


async def relabel(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, candidate_speaker: str
) -> InterviewCapture:
    interview = await consent.manageable_interview(ctx, db, interview_id)
    capture = await common.capture_for_interview(db, interview_id, for_update=True)
    if capture is None or capture.transcript_id is None:
        raise common.conflict("no_transcript", "Upload the meeting transcript first")
    if candidate_speaker not in (capture.speakers or []):
        raise common.invalid(
            "unknown_speaker",
            "Choose one of the detected speakers",
            fields={"candidate_speaker": "Unknown speaker"},
        )
    await map_speakers(db, interview, capture, capture.transcript_id, candidate_speaker=candidate_speaker)
    capture.speakers = []
    await db.flush()
    if capture.status in ("uploaded",):
        await _ready(db, capture)
    return capture


__all__ = [
    "AUDIO_TYPES",
    "audio_kind",
    "map_speakers",
    "reader_ctx",
    "relabel",
    "set_transcription_provider",
    "supersede_outputs",
    "transcribe",
    "transcription_provider",
    "upload_recording",
    "upload_transcript",
]
