# ruff: file-ignore[line-too-long]
"""Operations behind the Voice Screening Agent's tools (Epic 58 Part B). The tools pass the run subject: a session of
another Application is ``not_found``. No operation accepts or analyses audio; answers come only from candidate-speaker
transcript lines through Epic 57's shared ``extract_answers(source="voice")``."""

import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.voice import VoiceSession
from app.services.voice import common

VOICE_BLOCKLIST = re.compile(
    r"\b(voice|vocal|pitch|pace|paced|pauses?|paused|hesitat\w*|stutter\w*|sigh\w*|laugh\w*|honest\w*|dishonest\w*|"
    r"sincer\w*|lying|liar|truthful\w*|deceptive|credib\w*|sounded|sounds)\b",
    re.IGNORECASE,
)


async def load(
    db: AsyncSession, session_id: uuid.UUID, subject: tuple[str, uuid.UUID] | None, *, write: bool = False
) -> VoiceSession:
    row = await db.get(VoiceSession, session_id, with_for_update=write)
    if row is None or subject is None or subject[0] != "application" or row.application_id != subject[1]:
        raise NotFound("Voice session not found")
    return row


async def candidate_segments(db: AsyncSession, row: VoiceSession) -> int:
    from app.models.transcripts import TranscriptSegment

    if row.transcript_id is None:
        return 0
    return int(
        (
            await db.execute(
                select(func.count()).where(
                    TranscriptSegment.transcript_id == row.transcript_id,
                    TranscriptSegment.speaker_role == "candidate",
                )
            )
        ).scalar_one()
    )


async def session_view(db: AsyncSession, row: VoiceSession) -> dict[str, Any]:
    return {
        "status": row.status, "mode": row.mode, "provider_key": row.provider_key, "recording_allowed": row.recording_allowed,
        "recording_rule": row.recording_rule, "jurisdiction": row.jurisdiction, "has_transcript": row.transcript_id is not None,
        "candidate_segments": await candidate_segments(db, row), "questionnaire_id": row.questionnaire_id,
        "questionnaire_version": row.questionnaire_version,
    }  # fmt: skip


async def policy_view(db: AsyncSession, row: VoiceSession) -> dict[str, Any]:
    from app.services.recording_policy import gate

    rctx = common.reader_ctx(row.tenant_id, row.owner_user_id)
    confirmed = (row.confirmed_country, row.confirmed_region) if row.confirmed_country else None
    d = await gate.check(
        rctx,
        db,
        purpose=common.PURPOSE_RECORDING,  # type: ignore[arg-type]
        candidate_id=row.candidate_id,
        confirmed_jurisdiction=confirmed,
    )
    return {"rule": d.rule, "recording_allowed": d.recording_allowed, "reasons": list(d.reasons)}


async def transcript_text(ctx: RequestContext, db: AsyncSession, row: VoiceSession, max_chars: int) -> str:
    from app.services import transcripts_service

    if row.transcript_id is None:
        raise common.conflict("transcript_missing", "There is no transcript yet")
    return await transcripts_service.for_model(
        common.reader_ctx(row.tenant_id, row.owner_user_id, ctx), db, row.transcript_id, max_chars=max_chars
    )


async def request_consent(
    ctx: RequestContext, db: AsyncSession, row: VoiceSession, channel: str
) -> dict[str, Any]:
    from app.services.voice import consent

    return await consent.send_request(ctx, db, row, channel=channel, origin="agent")


async def schedule(ctx: RequestContext, db: AsyncSession, row: VoiceSession, at: Any) -> dict[str, Any]:
    from app.services.voice import provider_jobs

    return await provider_jobs.schedule(ctx, db, row, at)


async def _questionnaire(ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> None:
    from app.services import screening_agent_service

    if row.questionnaire_id is not None and row.questionnaire_version is not None:
        return
    if row.application_id is None:
        raise common.conflict("questionnaire_missing", "There is no questionnaire for this session")
    ver = await screening_agent_service.resolve_for_application(
        common.reader_ctx(row.tenant_id, row.owner_user_id, ctx), db, row.application_id
    )
    if ver is None:
        raise common.conflict("questionnaire_missing", "There is no questionnaire for this session")
    row.questionnaire_id, row.questionnaire_version = ver.questionnaire_id, ver.version
    await db.flush()


async def extract(
    ctx: RequestContext, db: AsyncSession, row: VoiceSession, run_id: uuid.UUID | None
) -> dict[str, Any]:
    """Answers from candidate-speaker lines only (Epic 57 ``extract_answers(source="voice")``)."""
    from app.schemas.screening_agent import ExtractAnswersIn
    from app.services import screening_agent_service

    if row.transcript_id is None or row.application_id is None:
        raise common.conflict("transcript_missing", "There is no transcript yet")
    if row.status in ("reviewed", "cancelled", "failed"):
        raise common.conflict("voice_session_closed", "This voice screen is closed", status=row.status)
    await _questionnaire(ctx, db, row)
    if await candidate_segments(db, row) == 0:
        await common.owner_task(
            db,
            row,
            f"Mark the candidate's lines in the transcript for {await common.candidate_name(db, row.candidate_id)}",
        )
        return {"answered": [], "unanswered_required": [], "needs_speaker_label": True}
    out = await screening_agent_service.extract_answers(
        ctx, db,
        ExtractAnswersIn(
            application_id=row.application_id, questionnaire_id=row.questionnaire_id, questionnaire_version=row.questionnaire_version,
            transcript_id=row.transcript_id, source="voice", run_id=run_id,
        ),
    )  # fmt: skip
    audit.record(
        "voice_session.answers_extracted", "voice_session", row.id, session=db, answers=len(out.answers)
    )
    return {
        "answered": [
            {"question_id": a.question_id, "normalized": a.normalized, "confidence": float(a.confidence), "start_ms": a.start_ms, "flags": list(a.flags)}
            for a in out.answers
        ],
        "unanswered_required": list(out.unanswered),
        "needs_speaker_label": False,
    }  # fmt: skip


def check_summary(texts: list[str]) -> None:
    from app.services import screening_agent_service

    for text in texts:
        if screening_agent_service.ops.SUMMARY_BLOCKLIST.search(text) or VOICE_BLOCKLIST.search(text):
            raise common.invalid(
                "summary_language",
                "The summary may only state answers and gaps; it cannot describe the person or their voice",
            )


async def hand_to_reviewer(ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> None:
    if row.status not in ("processing", "uploaded"):
        return
    row.status = "ready_for_review"
    await db.flush()
    common.fan_out(row.tenant_id, "voice_session_ready", {
        "event_id": f"voice_ready:{row.id}", "voice_session_id": str(row.id), "user_id": str(row.owner_user_id),
        "application_id": str(row.application_id) if row.application_id else None, "candidate_id": str(row.candidate_id),
    })  # fmt: skip
    await common.timeline(ctx, db, row, "Voice screen ready for review", "ready")


async def save_summary(
    ctx: RequestContext, db: AsyncSession, row: VoiceSession, summary: str, gaps: list[str]
) -> dict[str, Any]:
    if row.status not in ("processing", "uploaded", "ready_for_review"):
        raise common.conflict(
            "voice_session_not_processing", "This voice screen is not being processed", status=row.status
        )
    check_summary([summary, *gaps])
    row.summary = summary[:3000]
    row.gaps = gaps[:20]
    await db.flush()
    await hand_to_reviewer(ctx, db, row)
    return {"status": row.status}


# ================================================================ candidate updates (after review)
async def update_fields(db: AsyncSession, row: VoiceSession) -> list[dict[str, Any]]:
    from app.models.pipeline import Screening
    from app.services.pipeline import screening as p1

    if row.screening_id is None:
        return []
    screening = await db.get(Screening, row.screening_id)
    if screening is None:
        return []
    return [p.model_dump(mode="json") for p in await p1.proposals(db, screening)]


async def updates_version(db: AsyncSession, row: VoiceSession) -> str:
    fields = await update_fields(db, row)
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()[:32]


async def apply_updates(ctx: RequestContext, db: AsyncSession, row: VoiceSession) -> list[str]:
    from app.services.pipeline import screening as p1

    fields = [f["field"] for f in await update_fields(db, row)]
    if not fields or row.screening_id is None:
        raise common.invalid(
            "no_proposal", "There are no candidate updates to apply (review the answers first)"
        )
    out = await p1.apply_candidate_updates(ctx, db, row.screening_id, fields)
    return list(out.applied)


__all__ = [
    "VOICE_BLOCKLIST",
    "apply_updates",
    "candidate_segments",
    "check_summary",
    "extract",
    "hand_to_reviewer",
    "load",
    "policy_view",
    "request_consent",
    "save_summary",
    "schedule",
    "session_view",
    "transcript_text",
    "update_fields",
    "updates_version",
]
