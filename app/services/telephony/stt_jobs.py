# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Speech-to-text jobs (Epic 76, story 76.3, spec A5; D-6 Azure AI Speech).

* ``enqueue`` -- for a stored call recording (``voice_call``) or a P4 upload (``voice_upload``, through the transcription
  resolver) when ``telephony_settings.stt_enabled`` and an ``stt`` Installation is active (sandbox -> Fake). One job per
  (source, file).
* ``run`` (task ``telephony.transcribe``, queue ``telephony``): gates -- ``voice_screening`` consent active, Ecosystem Kill
  Switch off (else held), daily cost cap; dual-channel PCM16 WAV is split (``split_channels``: pure Python, no
  ``audioop`` / ffmpeg) and each channel transcribed (channel 1 ``recruiter``, channel 2 ``candidate``; long audio in
  <= 5 min chunks with offset timestamps); mono / non-WAV -> one pass with speaker ``unknown``. The transcript goes to
  the P4 transcripts domain and the session to ``mark_ready`` (extraction unchanged; no audio features analysed).
* Failure after 5 attempts -> ``failed`` and the P4 fallback (``uploaded`` + owner Task + ``voice_upload_needed``).
"""

import array
import io
import math
import sys
import uuid
import wave
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.events.telephony import SttJobQueued
from app.models.connectors import ProviderConnectorConfig
from app.models.telephony import SttJob
from app.models.voice import VoiceSession
from app.services.connectors.voice.bridge import billable_minutes
from app.services.connectors.voice.port import TranscriptDTO, TranscriptSegmentDTO
from app.services.telephony import common

MAX_ATTEMPTS = 5
CHUNK_SECONDS = 300
STT_CATEGORY = "stt"


# --------------------------------------------------------------------------------------------- audio helpers
def wav_info(data: bytes) -> tuple[int, int, int, bytes] | None:
    """``(channels, sample_width, rate, pcm)`` of a PCM WAV, else None."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None


def split_channels(data: bytes) -> tuple[int, bytes, bytes] | None:
    """De-interleave a 2-channel PCM16 WAV into ``(rate, channel_1_pcm, channel_2_pcm)``; None otherwise."""
    info = wav_info(data)
    if info is None:
        return None
    channels, width, rate, pcm = info
    if channels != 2 or width != 2:
        return None
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 4)])
    if sys.byteorder != "little":  # pragma: no cover - WAV is little-endian
        samples.byteswap()
    left, right = samples[0::2], samples[1::2]
    if sys.byteorder != "little":  # pragma: no cover - back to WAV byte order
        left.byteswap()
        right.byteswap()
    return rate, left.tobytes(), right.tobytes()


def mono_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def chunks(pcm: bytes, rate: int, seconds: int = CHUNK_SECONDS) -> list[tuple[int, bytes]]:
    """Split mono PCM16 on frame boundaries into ``(offset_ms, pcm)`` pieces of at most ``seconds``."""
    step = rate * 2 * seconds
    if step <= 0 or len(pcm) <= step:
        return [(0, pcm)]
    return [(int(i / 2 / rate * 1000), pcm[i : i + step]) for i in range(0, len(pcm), step)]


def audio_seconds(data: bytes) -> int | None:
    info = wav_info(data)
    if info is None:
        return None
    channels, width, rate, pcm = info
    if not rate or not channels or not width:
        return None
    return math.ceil(len(pcm) / (rate * channels * width))


def merge(parts: list[tuple[str, TranscriptDTO, int]]) -> tuple[list[TranscriptSegmentDTO], str | None]:
    """Relabel each part's segments with its speaker, shift by the offset and order by ``start_ms``."""
    out: list[TranscriptSegmentDTO] = []
    language: str | None = None
    for speaker, dto, offset in parts:
        language = language or dto.language
        for s in dto.segments:
            start = None if s.start_ms is None else s.start_ms + offset
            end = None if s.end_ms is None else s.end_ms + offset
            out.append(TranscriptSegmentDTO(speaker if speaker != "unknown" else s.speaker, start, end, s.text))  # type: ignore[arg-type]
    out.sort(key=lambda s: (s.start_ms if s.start_ms is not None else 0))
    return out, language


# --------------------------------------------------------------------------------------------- enqueue
async def stt_config(db: AsyncSession) -> ProviderConnectorConfig | None:
    from app.services.connectors.registry import active_config

    return await active_config(db, STT_CATEGORY, None)


async def enqueue(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: uuid.UUID,
    file_id: uuid.UUID,
    voice_session_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    channels: int | None = None,
) -> uuid.UUID | None:
    """Queue a transcription; None when automatic transcription is off or no STT provider is installed."""
    from app.services.telephony import settings as settings_mod

    s = await settings_mod.row(db)
    cfg = await stt_config(db)
    if not s.stt_enabled or cfg is None:
        return None
    job_id = uuid.uuid4()
    inserted = (
        await db.execute(
            pg_insert(SttJob)
            .values(
                id=job_id, tenant_id=common.tenant_of(db), source_kind=source_kind, source_id=source_id,
                voice_session_id=voice_session_id, candidate_id=candidate_id, file_id=file_id,
                installation_id=cfg.installation_id, provider_key=cfg.provider_key[:20], status="queued",
                language_hints=list(s.stt_language_hints or []), channels=channels,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "source_kind", "source_id", "file_id"])
            .returning(SttJob.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return (
            await db.execute(
                select(SttJob.id).where(SttJob.source_kind == source_kind, SttJob.source_id == source_id, SttJob.file_id == file_id)
            )
        ).scalar_one()
    emit(db, SttJobQueued(tenant_id=common.tenant_of(db), job_id=inserted))
    return inserted


class SttJobProvider:
    """The P4 transcription resolver's provider: queues an ``stt_jobs`` row for an uploaded recording."""

    async def enqueue_for_session(self, ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> uuid.UUID | None:
        if row.recording_file_id is None:
            return None
        return await enqueue(
            ctx, db, source_kind="voice_upload", source_id=row.id, file_id=row.recording_file_id,
            voice_session_id=row.id, candidate_id=row.candidate_id,
        )  # fmt: skip


async def resolver(ctx: RequestContext, db: AsyncSession) -> Any | None:  # ruff: ignore[unused-function-argument]
    """Registered with P4 ``voice.uploads.register_transcription_resolver``."""
    from app.services.telephony import settings as settings_mod

    s = await settings_mod.row(db)
    if not (s.stt_enabled and s.auto_transcribe_uploads) or await stt_config(db) is None:
        return None
    return SttJobProvider()


# --------------------------------------------------------------------------------------------- run
async def _transcribe(adapter: Any, audio: bytes, media_type: str, hints: list[str]) -> TranscriptDTO:
    hint = hints[0] if hints else None
    fn = getattr(adapter, "transcribe_with", None)
    if fn is not None:
        return await fn(audio, media_type, hint, locales=tuple(hints) or None)  # type: ignore[no-any-return]
    return await adapter.transcribe(audio, media_type, hint)  # type: ignore[no-any-return]


async def transcribe_audio(adapter: Any, data: bytes, media_type: str, hints: list[str]) -> tuple[list[TranscriptSegmentDTO], str | None]:
    split = split_channels(data) if media_type in ("audio/wav", "audio/x-wav", "audio/wave") else None
    parts: list[tuple[str, TranscriptDTO, int]] = []
    if split is not None:
        rate, ch1, ch2 = split
        for speaker, pcm in (("recruiter", ch1), ("candidate", ch2)):
            for offset, piece in chunks(pcm, rate):
                parts.append((speaker, await _transcribe(adapter, mono_wav(piece, rate), "audio/wav", hints), offset))
    else:
        info = wav_info(data) if media_type.startswith("audio/") else None
        if info is not None and info[0] == 1 and info[1] == 2:
            for offset, piece in chunks(info[3], info[2]):
                parts.append(("unknown", await _transcribe(adapter, mono_wav(piece, info[2]), "audio/wav", hints), offset))
        else:
            parts.append(("unknown", await _transcribe(adapter, data, media_type, hints), 0))
    return merge(parts)


async def run(ctx: RequestContext, db: AsyncSession, job_id: uuid.UUID) -> str:
    from app.schemas.transcripts import TranscriptSegmentIn
    from app.services import (
        ecosystem_service,
        files_service,
        metering_service,
        transcripts_service,
        voice_service,
    )
    from app.services.connectors.registry import build_for
    from app.services.telephony import costs
    from app.services.telephony import settings as settings_mod
    from app.services.voice.common import PURPOSE_CALL, has_consent, reader_ctx

    job = (await db.execute(select(SttJob).where(SttJob.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None or job.status not in ("queued", "running"):
        return job.status if job is not None else "missing"
    if (await ecosystem_service.is_suspended(job.tenant_id)).suspended:
        return "held"
    session = await db.get(VoiceSession, job.voice_session_id) if job.voice_session_id else None
    if session is None or job.candidate_id is None or not await has_consent(db, job.candidate_id, PURPOSE_CALL):
        job.status, job.error_code = "skipped", "consent_missing"
        await db.flush()
        return job.status
    s = await settings_mod.row(db)
    if await costs.today(db) >= s.daily_cost_cap_minor > 0:
        job.status, job.error_code = "skipped", "cost_cap_reached"
        await db.flush()
        await voice_service.on_provider_event(ctx, db, session.id, "transcribe_failed")
        return job.status
    job.status = "running"
    job.attempts += 1
    await db.flush()
    try:
        file = await files_service.get_file(db, job.file_id)
        if file is None:
            raise LookupError("recording file missing")
        data = await files_service.read_file(file)
        cfg = await stt_config(db)
        adapter = await build_for(ctx, db, STT_CATEGORY, cfg)
        segments, language = await transcribe_audio(adapter, data, getattr(file, "content_type", None) or "audio/wav", list(job.language_hints or []))
    except Exception as exc:
        logger.warning("STT job {} attempt {} failed: {}", job.id, job.attempts, type(exc).__name__)
        job.error_code = type(exc).__name__[:40]
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            await db.flush()
            await voice_service.on_provider_event(ctx, db, session.id, "transcribe_failed")
            return job.status
        job.status = "queued"
        await db.flush()
        return "retry"  # the attempt is committed; the task re-queues with back-off
    seconds = audio_seconds(data) or 0
    job.audio_seconds = seconds
    minutes = billable_minutes(seconds)
    job.billable_minutes = minutes
    rate = int((cfg.settings or {}).get("stt_rate_per_minute_minor") or 0) if cfg is not None else 0
    job.cost_estimate_minor = minutes * rate
    if not segments:
        job.status, job.error_code, job.completed_at = "failed", "empty_transcript", common.now()
        await db.flush()
        await voice_service.on_provider_event(ctx, db, session.id, "transcribe_failed")
        return job.status
    rctx = reader_ctx(session.tenant_id, session.owner_user_id, ctx)
    t = await transcripts_service.create(
        rctx, db, source_kind="voice_session", source_id=session.id, candidate_id=session.candidate_id,
        application_id=session.application_id, language=language,
    )  # fmt: skip
    segs = [TranscriptSegmentIn(speaker_role=x.speaker, start_ms=x.start_ms, end_ms=x.end_ms, text=x.text) for x in segments]
    await transcripts_service.append_segments(rctx, db, t.id, segs, final=True)
    job.transcript_id = t.id
    job.status = "completed"
    job.completed_at = common.now()
    job.error_code = None
    await db.flush()
    if minutes:
        await metering_service.record(RequestContext(tenant_id=job.tenant_id), "transcription_minute", minutes, job.installation_id)
    if session.transcript_id is None:
        await voice_service.on_provider_event(ctx, db, session.id, "transcript_ready", transcript_id=t.id)
    return job.status


async def mark_failed(db: AsyncSession, job_id: uuid.UUID) -> None:
    from app.services import voice_service

    job = await db.get(SttJob, job_id)
    if job is None or job.status in ("completed", "failed", "skipped"):
        return
    job.status = "failed"
    await db.flush()
    if job.voice_session_id is not None:
        await voice_service.on_provider_event(RequestContext(tenant_id=job.tenant_id, actor_kind="system"), db, job.voice_session_id, "transcribe_failed")
