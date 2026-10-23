# ruff: file-ignore[line-too-long]
"""Recruiter review of extracted answers (Epic 57 story 57.5, FR-291; spec A6). Text (conversation) and voice
(Epic 58 ``voice_session_id``) answers share this path.

* ``decide_answer`` -- accept / edit / reject one answer (human only, ★ ``screening_answer``). Low-confidence answers
  are never accepted automatically: a person decides each one.
* ``complete`` -- human only; every current answer decided. The final values go into the P1 ``screenings`` row through
  the additive ``apply_agent_review`` (which calls the existing P1 ``review``); the outcome is then set with the
  unchanged human-only P1 ``POST /screenings/{id}/outcome``. Emits ``ScreeningAnswersReviewed``.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.screening_agent import ScreeningAnswersReviewed
from app.models.screening_agent import ScreeningAnswer, ScreeningConversation, ScreeningQuestionnaireVersion
from app.schemas.screening_agent import AnswerDecisionIn, AnswerOut, KnockoutRule, ReviewOut
from app.services.screening_agent import answers as answers_mod
from app.services.screening_agent.common import (
    conflict,
    invalid,
    now,
    required_ids,
    settings_row,
    threshold,
    user_id_of,
)


async def _rows(
    db: AsyncSession, conversation_id: uuid.UUID | None, voice_session_id: uuid.UUID | None
) -> list[ScreeningAnswer]:
    stmt = select(ScreeningAnswer).where(ScreeningAnswer.superseded_at.is_(None))
    if conversation_id is not None:
        stmt = stmt.where(ScreeningAnswer.conversation_id == conversation_id)
    else:
        stmt = stmt.where(
            ScreeningAnswer.voice_session_id == voice_session_id, ScreeningAnswer.source == "voice"
        )
    return list((await db.execute(stmt.order_by(ScreeningAnswer.created_at))).scalars())


async def _questions(db: AsyncSession, version_ids: set[uuid.UUID]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for vid in version_ids:
        ver = await db.get(ScreeningQuestionnaireVersion, vid)
        for q in (ver.questions if ver is not None else []) or []:
            out[str(q["id"])] = q
    return out


def _answer_out(a: ScreeningAnswer, q: dict[str, Any] | None, limit: Decimal) -> AnswerOut:
    q = q or {}
    return AnswerOut(
        id=a.id, question_id=a.question_id, question_label=str(q.get("label") or "Question"),
        question_type=str(q.get("type") or "text"), unit=q.get("unit"), required=bool(q.get("required")),
        source=a.source, answer_text=a.answer_text, normalized=a.normalized, confidence=a.confidence,
        low_confidence=a.confidence is None or a.confidence < limit, flags=list(a.flags or []),
        knockout=KnockoutRule.model_validate(q["knockout"]) if q.get("knockout") else None,
        status=a.status, edited_value=a.edited_value, segment_refs=list(a.segment_refs or []),
        start_ms=a.start_ms, reviewed_by=a.reviewed_by, reviewed_at=a.reviewed_at,
    )  # fmt: skip


async def _check_access(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> None:
    from app.services.pipeline.common import READ, application_for

    await application_for(ctx, db, application_id, READ)


async def list_answers(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID | None = None,
    voice_session_id: uuid.UUID | None = None,
) -> list[AnswerOut]:
    rows = await _rows(db, conversation_id, voice_session_id)
    if conversation_id is not None:
        conv = await db.get(ScreeningConversation, conversation_id)
        if conv is None:
            raise NotFound("Screening conversation not found")
        await _check_access(ctx, db, conv.application_id)
    elif rows:
        await _check_access(ctx, db, rows[0].application_id)
    limit = threshold(await settings_row(db))
    qs = await _questions(db, {r.questionnaire_version_id for r in rows})
    return [_answer_out(r, qs.get(str(r.question_id)), limit) for r in rows]


async def get(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID | None = None,
    voice_session_id: uuid.UUID | None = None,
) -> ReviewOut:
    if (conversation_id is None) == (voice_session_id is None):
        raise invalid("review_target", "Give exactly one of conversation_id / voice_session_id")
    answers = await list_answers(ctx, db, conversation_id=conversation_id, voice_session_id=voice_session_id)
    limit = threshold(await settings_row(db))
    conv = await db.get(ScreeningConversation, conversation_id) if conversation_id else None
    if conversation_id is not None and conv is None:
        raise NotFound("Screening conversation not found")
    if conv is None and not answers:
        raise NotFound("No answers to review")
    version_id = conv.questionnaire_version_id if conv is not None else None
    rows = await _rows(db, conversation_id, voice_session_id)
    if version_id is None and rows:
        version_id = rows[0].questionnaire_version_id
    ver = await db.get(ScreeningQuestionnaireVersion, version_id) if version_id else None
    answered = {a.question_id for a in answers if a.status != "rejected" and a.normalized is not None}
    unanswered = [
        q for q in required_ids(list(ver.questions if ver is not None else [])) if q not in answered
    ]
    status = (
        conv.status
        if conv is not None
        else ("reviewed" if answers and all(a.reviewed_at for a in answers) else "ready_for_review")
    )
    decided = all(a.status != "unconfirmed" for a in answers)
    return ReviewOut(
        conversation_id=conversation_id, voice_session_id=voice_session_id,
        application_id=conv.application_id if conv is not None else rows[0].application_id,
        screening_id=conv.screening_id if conv is not None else None, status=status, threshold=limit,
        summary=conv.summary if conv is not None else None, gaps=list(conv.gaps or []) if conv is not None else [],
        suggested_next=conv.suggested_next if conv is not None else None, unanswered_required=unanswered,
        answers=answers, can_complete=decided and status in ("completed", "ready_for_review"),
    )  # fmt: skip


async def decide_answer(
    ctx: RequestContext, db: AsyncSession, answer_id: uuid.UUID, data: AnswerDecisionIn
) -> AnswerOut:
    require_human(actor_from_ctx(ctx))
    row = (
        await db.execute(select(ScreeningAnswer).where(ScreeningAnswer.id == answer_id).with_for_update())
    ).scalar_one_or_none()
    if row is None or row.superseded_at is not None:
        raise NotFound("Answer not found")
    await _check_access(ctx, db, row.application_id)
    if row.conversation_id is not None:
        conv = await db.get(ScreeningConversation, row.conversation_id)
        if conv is not None and conv.status == "reviewed":
            raise conflict("review_completed", "The review is already complete")
    qs = await _questions(db, {row.questionnaire_version_id})
    q = qs.get(str(row.question_id)) or {}
    if data.decision == "edit":
        value = (data.edited_value or {}).get("value")
        normalized = answers_mod.normalize(q, value) if q else None
        if normalized is None:
            raise invalid(
                "invalid_value", "The edited value does not fit the question", fields={"value": "Invalid"}
            )
        row.edited_value = normalized
        row.status = "edited"
    else:
        row.status = "accepted" if data.decision == "accept" else "rejected"
        row.edited_value = None
        if row.status == "accepted" and row.normalized is None:
            raise invalid("invalid_value", "This answer has no valid value: edit or reject it")
    row.reviewed_by = user_id_of(ctx)
    row.reviewed_at = now()
    await db.flush()
    limit = threshold(await settings_row(db))
    return _answer_out(row, q, limit)


async def complete(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID | None = None,
    voice_session_id: uuid.UUID | None = None,
) -> ReviewOut:
    from app.schemas.pipeline import ScreeningSummary
    from app.services.pipeline import screening as p1

    require_human(actor_from_ctx(ctx))
    rows = await _rows(db, conversation_id, voice_session_id)
    conv = None
    if conversation_id is not None:
        conv = (
            await db.execute(
                select(ScreeningConversation)
                .where(ScreeningConversation.id == conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conv is None:
            raise NotFound("Screening conversation not found")
        await _check_access(ctx, db, conv.application_id)
        if conv.status != "completed":
            raise conflict(
                "conversation_not_completed",
                "Only a completed conversation can be reviewed",
                status=conv.status,
            )
    elif not rows:
        raise NotFound("No answers to review")
    else:
        await _check_access(ctx, db, rows[0].application_id)
    undecided = [str(r.question_id) for r in rows if r.status == "unconfirmed"]
    if undecided:
        raise conflict(
            "answers_undecided", "Accept, edit or reject every answer first", question_ids=undecided
        )
    qs = await _questions(db, {r.questionnaire_version_id for r in rows})
    final: dict[str, Any] = {}
    for r in rows:
        q = qs.get(str(r.question_id))
        if q is None or r.status == "rejected":
            continue
        value = (r.edited_value if r.status == "edited" else r.normalized) or {}
        if value.get("value") is not None:
            final[q["key"]] = value["value"]
    application_id = conv.application_id if conv is not None else rows[0].application_id
    screening_id = conv.screening_id if conv is not None else None
    if screening_id is None:
        snapshot = [
            {k: v for k, v in q.items() if k in ("key", "label", "type", "options", "required", "maps_to")}
            for q in qs.values()
        ]
        screening_id = await p1.record_agent_result(
            ctx, db, application_id, questions_snapshot=snapshot, answers=final,
            summary=ScreeningSummary(summary="Voice screening answers reviewed by a recruiter."), source_ref="",
        )  # fmt: skip
        if conv is not None:
            conv.screening_id = screening_id
    reviewer = user_id_of(ctx)
    await p1.apply_agent_review(ctx, db, screening_id, final, reviewer)
    if conv is not None:
        conv.status = "reviewed"
        conv.reviewed_by = reviewer
        conv.reviewed_at = now()
    limit = threshold(await settings_row(db))
    emit(db, ScreeningAnswersReviewed(
        tenant_id=rows[0].tenant_id if rows else conv.tenant_id,  # type: ignore[union-attr]
        conversation_id=conversation_id, application_id=application_id,
        accepted=sum(1 for r in rows if r.status == "accepted"), edited=sum(1 for r in rows if r.status == "edited"),
        low_confidence=sum(1 for r in rows if r.confidence is None or r.confidence < limit), reviewer_id=reviewer,
        voice_session_id=voice_session_id,
    ))  # fmt: skip
    audit.record("screening_review.completed", "application", application_id, session=db, answers=len(rows))
    await db.flush()
    out = await get(ctx, db, conversation_id=conversation_id, voice_session_id=voice_session_id)
    return out.model_copy(update={"screening_id": screening_id})


async def erase_voice_answers(ctx: RequestContext, db: AsyncSession, voice_session_id: uuid.UUID) -> int:  # ruff: ignore[unused-function-argument]
    result = await db.execute(
        delete(ScreeningAnswer).where(
            ScreeningAnswer.voice_session_id == voice_session_id, ScreeningAnswer.source == "voice"
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["complete", "decide_answer", "erase_voice_answers", "get", "list_answers"]
