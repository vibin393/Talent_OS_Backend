# ruff: file-ignore[line-too-long]
"""``extract_answers`` (Epic 57 A4; foundation contract stub filled -- signature and DTOs frozen by Epic 53, used by 57 and
Epic 58 voice screening).

1. The approved questionnaire version (``questionnaire_id`` + ``questionnaire_version``) and the transcript; for
   ``source="voice"`` only candidate-speaker segments are sent.
2. One fast-tier gateway call (``agent_screening`` / ``screening_extract.v1``, Haiku, coordinator decision 7) with the
   transcript as an untrusted document. Gateway failure -> empty result (``answers=()``, every question unanswered).
3. Normalisation per type (P1 ``normalize_answers``), ``invalid_value`` / ``knockout`` / ``low_confidence`` flags.
4. Upsert ``screening_answers`` (supersede per ``(transcript_id, question_id)``).
``ExtractionOut`` forbids extra fields: the model cannot return anything about tone, emotion or personality.
"""

import json
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.pipeline import Application
from app.models.screening_agent import ScreeningConversation, ScreeningQuestionnaireVersion
from app.schemas.screening_agent import ExtractAnswersIn, ExtractedAnswer, ExtractedAnswersOut, ExtractionOut
from app.services.screening_agent import answers as answers_mod
from app.services.screening_agent.common import FEATURE, settings_row, threshold

PROMPT_ID = "screening_extract.v1"
FAKE_SCRIPT_KEY = "screening_extract"
MAX_TRANSCRIPT_CHARS = 60_000
VOICE_FEATURE = "agent_voice_screening"  # Epic 58 hook: voice extraction is gated / billed as voice screening


async def _reader_ctx(db: AsyncSession, app: Application, ctx: RequestContext) -> RequestContext:
    """Transcript reads are access-checked on the candidate: use the Application owner's principal (actor unchanged)."""
    import dataclasses

    from app.services import entity_refs

    principal = await entity_refs.principal_for_user(db, app.tenant_id, app.owner_id)
    return dataclasses.replace(ctx, principal=principal) if principal is not None else ctx


async def _transcript_text(
    ctx: RequestContext, db: AsyncSession, transcript_id: uuid.UUID, source: str
) -> tuple[str, dict[int, int | None]]:
    from app.services import transcripts_service

    out = await transcripts_service.get(ctx, db, transcript_id, include_segments=True)
    lines: list[str] = []
    starts: dict[int, int | None] = {}
    used = 0
    for seg in out.segments:
        if source == "voice" and seg.speaker_role != "candidate":
            continue
        line = f"[{seg.seq}] {seg.speaker_role.upper()}: {seg.text}"
        if used + len(line) > MAX_TRANSCRIPT_CHARS:
            break
        lines.append(line)
        starts[seg.seq] = seg.start_ms
        used += len(line) + 1
    return "\n".join(lines), starts


def _questions_for_model(questions: list[dict[str, Any]]) -> str:
    return json.dumps(
        [
            {
                k: q[k]
                for k in ("id", "label", "type", "unit", "currency", "options", "required")
                if q.get(k) is not None
            }
            for q in questions
        ],
        ensure_ascii=False,
    )


async def extract_answers(
    ctx: RequestContext, db: AsyncSession, req: ExtractAnswersIn
) -> ExtractedAnswersOut:
    """Extract questionnaire answers from a transcript (text or voice screening)."""
    from app.services import ai
    from app.services.ai import fake_llm

    app = await db.get(Application, req.application_id)
    if app is None:
        raise NotFound("Application not found")
    ver = (
        await db.execute(
            select(ScreeningQuestionnaireVersion).where(
                ScreeningQuestionnaireVersion.questionnaire_id == req.questionnaire_id,
                ScreeningQuestionnaireVersion.version == req.questionnaire_version,
            )
        )
    ).scalar_one_or_none()
    if ver is None:
        raise NotFound("Questionnaire version not found")
    questions = list(ver.questions or [])
    by_id = {str(q["id"]): q for q in questions}
    rctx = await _reader_ctx(db, app, ctx)
    text, starts = await _transcript_text(rctx, db, req.transcript_id, req.source)
    conversation_id = voice_session_id = None
    if req.source == "text":
        conv = (
            await db.execute(
                select(ScreeningConversation).where(ScreeningConversation.transcript_id == req.transcript_id)
            )
        ).scalar_one_or_none()
        if conv is None:
            raise NotFound("Screening conversation not found")
        conversation_id = conv.id
    else:
        from app.models.voice import VoiceSession

        voice_session_id = (
            await db.execute(select(VoiceSession.id).where(VoiceSession.transcript_id == req.transcript_id))
        ).scalar_one_or_none()
    limit = threshold(await settings_row(db))
    result = None
    if text.strip():
        token = fake_llm.CURRENT_PROMPT.set(FAKE_SCRIPT_KEY)
        try:
            result = await ai.run(
                ctx, db, FEATURE if req.source == "text" else VOICE_FEATURE, PROMPT_ID, inputs={"questions": _questions_for_model(questions), "source": req.source},
                untrusted={"transcript": text}, output_schema=ExtractionOut, subject=("candidate", app.candidate_id),
                input_ref=f"transcript:{req.transcript_id}", model_tier="fast",
            )  # fmt: skip
        finally:
            fake_llm.CURRENT_PROMPT.reset(token)
    if result is None or not result.ok or not isinstance(result.data, ExtractionOut):
        return ExtractedAnswersOut(
            application_id=app.id, transcript_id=req.transcript_id, answers=(), summary="",
            unanswered=tuple(uuid.UUID(str(q["id"])) for q in questions),
            ai_action_id=result.action_id if result is not None else None,
        )  # fmt: skip
    for item in result.data.answers:
        q = by_id.get(str(item.question_id))
        if q is None:
            continue  # the model may only answer questions of this version
        confidence = Decimal(str(item.confidence)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        normalized = answers_mod.normalize(q, item.value)
        if normalized is None:
            confidence = min(confidence, answers_mod.INVALID_CAP)
        refs = [r for r in item.segment_refs if r in starts]
        await answers_mod.upsert(
            db, tenant_id=app.tenant_id, candidate_id=app.candidate_id, application_id=app.id, source=req.source,
            conversation_id=conversation_id, voice_session_id=voice_session_id, transcript_id=req.transcript_id,
            version_id=ver.id, question=q, answer_text=item.quote, normalized=normalized, confidence=confidence,
            segment_refs=refs, start_ms=starts.get(refs[0]) if refs else None,
            flags=answers_mod.flags_for(q, normalized, confidence, limit), ai_action_id=result.action_id,
        )  # fmt: skip
    rows = await answers_mod.current(db, req.transcript_id)
    answered = {str(r.question_id) for r in rows if r.status != "rejected"}
    return ExtractedAnswersOut(
        application_id=app.id,
        transcript_id=req.transcript_id,
        answers=tuple(
            ExtractedAnswer(
                question_id=r.question_id, answer_text=r.answer_text or "", normalized=r.normalized,
                confidence=r.confidence if r.confidence is not None else Decimal(0), segment_refs=tuple(r.segment_refs or ()),
                start_ms=r.start_ms, flags=tuple(r.flags or ()),
            )
            for r in rows
            if str(r.question_id) in by_id
        ),
        summary="",
        unanswered=tuple(uuid.UUID(str(q["id"])) for q in questions if str(q["id"]) not in answered),
        ai_action_id=result.action_id,
    )  # fmt: skip


__all__ = ["extract_answers"]
