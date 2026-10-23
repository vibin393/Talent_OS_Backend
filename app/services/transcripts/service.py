# ruff: file-ignore[line-too-long]
"""Transcripts (Epic 53, AD-66, spec 0.13). Owner of ``transcripts`` / ``transcript_segments``.

* ``create`` opens a ``pending`` transcript; ``append_segments`` appends (``final`` -> ``ready`` + ``TranscriptReady``).
  Caps: 2,000 segments and 1 MB of text per transcript (``transcript_too_large``).
* ``ingest_upload`` reads a stored, clean file through ``files_service`` (TXT / VTT / SRT / DOCX, <= 5 MB) and
  creates a ``ready`` transcript; an unreadable file is ``transcript_unparseable``.
* ``get`` / ``for_model`` need read access to the linked Candidate (the candidate read scope; there is no
  ``transcript:read`` permission). ``for_model`` renders ``[mm:ss] ROLE: text`` lines wrapped as untrusted input.
* ``erase`` deletes the segments (and the uploaded file), keeps the header as ``erased`` and audits the reason.

Segment text is never logged or copied into ``audit_logs`` (``transcript_segments`` is not audited).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.transcripts import TranscriptReady
from app.models.transcripts import SOURCE_KINDS, Transcript, TranscriptSegment
from app.schemas.transcripts import TranscriptOut, TranscriptSegmentIn, TranscriptSegmentOut
from app.services import ai, entity_refs, files_service
from app.services.transcripts import parsers

MAX_SEGMENTS = 2000
MAX_TEXT_BYTES = 1_000_000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_MODEL_CHARS = 60_000
TRUNCATED = "[transcript truncated]"


# ================================================================ helpers
def _out(row: Transcript, segments: Sequence[TranscriptSegment] = ()) -> TranscriptOut:
    return TranscriptOut(
        id=row.id,
        source_kind=row.source_kind,
        source_id=row.source_id,
        candidate_id=row.candidate_id,
        application_id=row.application_id,
        interview_id=row.interview_id,
        language=row.language,
        status=row.status,
        segment_count=row.segment_count,
        file_id=row.file_id,
        created_by_kind=row.created_by_kind,
        created_by_id=row.created_by_id,
        attested_by=row.attested_by,
        attested_at=row.attested_at,
        erased_at=row.erased_at,
        created_at=row.created_at,
        segments=tuple(
            TranscriptSegmentOut(
                seq=s.seq,
                speaker_role=s.speaker_role,
                speaker_ref=s.speaker_ref,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                text=s.text,
            )
            for s in segments
        ),
    )


async def _load(
    ctx: RequestContext, db: AsyncSession, transcript_id: uuid.UUID, *, lock: bool = False
) -> Transcript:
    row = await db.get(Transcript, transcript_id, with_for_update=lock)
    if row is None:
        raise NotFound("Transcript not found")
    if row.candidate_id is not None:
        await entity_refs.require_readable(ctx, db, "candidate", row.candidate_id)
    return row


async def _segments(db: AsyncSession, transcript_id: uuid.UUID) -> list[TranscriptSegment]:
    stmt = select(TranscriptSegment).where(TranscriptSegment.transcript_id == transcript_id)
    return list((await db.execute(stmt.order_by(TranscriptSegment.seq))).scalars())


def _too_large(message: str, **details: int) -> ValidationFailed:
    return ValidationFailed(message, code="transcript_too_large", details=dict(details))


def _ready_event(row: Transcript) -> TranscriptReady:
    return TranscriptReady(
        tenant_id=row.tenant_id,
        transcript_id=row.id,
        source_kind=row.source_kind,
        source_id=row.source_id,
        candidate_id=row.candidate_id,
        application_id=row.application_id,
        interview_id=row.interview_id,
    )


# ================================================================ public API
async def create(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    interview_id: uuid.UUID | None = None,
    language: str | None = None,
    file_id: uuid.UUID | None = None,
    attested_by: uuid.UUID | None = None,
) -> TranscriptOut:
    """Open a ``pending`` transcript (no segments yet)."""
    if source_kind not in SOURCE_KINDS:
        raise ValidationFailed("Unknown transcript source", code="invalid_source_kind")
    if candidate_id is not None:
        await entity_refs.require_readable(ctx, db, "candidate", candidate_id)
    tenant_id = entity_refs.session_tenant(db)
    actor = actor_from_ctx(ctx)
    row = Transcript(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        source_kind=source_kind,
        source_id=source_id,
        candidate_id=candidate_id,
        application_id=application_id,
        interview_id=interview_id,
        language=language,
        status="pending",
        segment_count=0,
        file_id=file_id,
        created_by_kind=actor.kind,
        created_by_id=actor.id,
        attested_by=attested_by,
        attested_at=datetime.now(UTC) if attested_by is not None else None,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _out(row)


async def append_segments(
    ctx: RequestContext,
    db: AsyncSession,
    transcript_id: uuid.UUID,
    segments: Sequence[TranscriptSegmentIn],
    *,
    final: bool = False,
) -> TranscriptOut:
    """Append segments to a ``pending`` transcript; ``final`` marks it ``ready`` and emits ``TranscriptReady``."""
    row = await _load(ctx, db, transcript_id, lock=True)
    if row.status != "pending":
        raise ConflictError("The transcript is no longer open", code="transcript_not_pending")
    items = [
        s if isinstance(s, TranscriptSegmentIn) else TranscriptSegmentIn.model_validate(s) for s in segments
    ]
    total = row.segment_count + len(items)
    if total > MAX_SEGMENTS:
        raise _too_large(f"A transcript holds at most {MAX_SEGMENTS} segments", max_segments=MAX_SEGMENTS)
    existing_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(func.octet_length(TranscriptSegment.text)), 0)).where(
                TranscriptSegment.transcript_id == row.id
            )
        )
    ).scalar_one()
    new_bytes = sum(len(s.text.encode()) for s in items)
    if int(existing_bytes) + new_bytes > MAX_TEXT_BYTES:
        raise _too_large("A transcript holds at most 1 MB of text", max_bytes=MAX_TEXT_BYTES)
    if final and total == 0:
        raise ValidationFailed("A transcript needs at least one segment", code="transcript_empty")
    for offset, s in enumerate(items):
        db.add(
            TranscriptSegment(
                id=uuid.uuid4(),
                tenant_id=row.tenant_id,
                transcript_id=row.id,
                seq=row.segment_count + offset + 1,
                speaker_role=s.speaker_role,
                speaker_ref=s.speaker_ref,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                text=s.text,
            )
        )
    row.segment_count = total
    if final:
        row.status = "ready"
    await db.flush()
    if final:
        emit(db, _ready_event(row))
    return _out(row)


async def ingest_upload(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    file_id: uuid.UUID,
    source_kind: str = "upload",
    candidate_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    interview_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    attested_by: uuid.UUID | None = None,
) -> TranscriptOut:
    """Parse a stored transcript file (TXT / VTT / SRT / DOCX) into a ``ready`` transcript."""
    file = await files_service.get_file(db, file_id)
    if file is None or file.deleted_at is not None:
        raise NotFound("File not found")
    if file.scan_status not in files_service.DOWNLOADABLE_SCAN_STATUSES:
        raise ValidationFailed("The file has not passed the malware scan", code="file_not_clean")
    if file.size_bytes > MAX_UPLOAD_BYTES:
        raise ValidationFailed(
            "Transcript files are limited to 5 MB", code="file_too_large", details={"max_mb": 5}
        )
    data = await files_service.read_file(file)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationFailed(
            "Transcript files are limited to 5 MB", code="file_too_large", details={"max_mb": 5}
        )
    try:
        fmt, parsed = parsers.parse(data, file.original_name)
    except parsers.TranscriptParseError as exc:
        raise ValidationFailed(
            "The transcript file could not be read",
            code="transcript_unparseable",
            details={"reason": str(exc)},
        ) from exc
    if len(parsed) > MAX_SEGMENTS:
        raise _too_large(f"A transcript holds at most {MAX_SEGMENTS} segments", max_segments=MAX_SEGMENTS)
    created = await create(
        ctx,
        db,
        source_kind=source_kind,
        source_id=source_id,
        candidate_id=candidate_id,
        application_id=application_id,
        interview_id=interview_id,
        file_id=file_id,
        attested_by=attested_by,
    )
    segments = [
        TranscriptSegmentIn(speaker_role=p.speaker_role, start_ms=p.start_ms, end_ms=p.end_ms, text=p.text)
        for p in parsed
    ]
    out = await append_segments(ctx, db, created.id, segments, final=True)
    logger.info(
        "Transcript {} ingested from file {} ({}, {} segments)", out.id, file_id, fmt, out.segment_count
    )
    return out


async def get(
    ctx: RequestContext, db: AsyncSession, transcript_id: uuid.UUID, *, include_segments: bool = True
) -> TranscriptOut:
    row = await _load(ctx, db, transcript_id)
    return _out(row, await _segments(db, row.id) if include_segments else ())


async def for_model(
    ctx: RequestContext, db: AsyncSession, transcript_id: uuid.UUID, *, max_chars: int = MAX_MODEL_CHARS
) -> str:
    """``[mm:ss] ROLE: text`` lines (at most ``max_chars``) wrapped as an untrusted document for a model."""
    row = await _load(ctx, db, transcript_id)
    if row.status == "erased":
        raise ConflictError("The transcript was erased", code="transcript_erased")
    lines: list[str] = []
    used = 0
    for seg in await _segments(db, row.id):
        line = f"[{parsers.format_ts(seg.start_ms)}] {seg.speaker_role.upper()}: {seg.text}"
        if used + len(line) + 1 > max_chars:
            lines.append(TRUNCATED)
            break
        lines.append(line)
        used += len(line) + 1
    return ai.gateway.wrap_untrusted(f"transcript:{row.id}", "\n".join(lines))


async def erase(
    ctx: RequestContext, db: AsyncSession, transcript_id: uuid.UUID, *, reason: str
) -> TranscriptOut:
    """Delete the segments (and the uploaded source file); the header stays as ``erased``."""
    row = await _load(ctx, db, transcript_id, lock=True)
    await erase_rows(db, [row], reason=reason)
    return _out(row)


async def erase_rows(db: AsyncSession, rows: Sequence[Transcript], *, reason: str) -> int:
    """Erase loaded transcripts without an access check (erasure / retention callers). Returns segments deleted."""
    if not rows:
        return 0
    ids = [r.id for r in rows]
    result = await db.execute(delete(TranscriptSegment).where(TranscriptSegment.transcript_id.in_(ids)))
    now = datetime.now(UTC)
    for row in rows:
        if row.file_id is not None:
            await files_service.purge_file(db, row.file_id)
        if row.status != "erased":
            row.status = "erased"
            row.erased_at = now
        row.segment_count = 0
        audit.record("transcript.erased", "transcript", row.id, session=db, reason=reason)
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "MAX_MODEL_CHARS",
    "MAX_SEGMENTS",
    "MAX_TEXT_BYTES",
    "MAX_UPLOAD_BYTES",
    "append_segments",
    "create",
    "erase",
    "erase_rows",
    "for_model",
    "get",
    "ingest_upload",
]
