# ruff: noqa: E501
"""Extracted answers (Epic 57 A4 / story 57.5): normalisation, flags and the supersede-on-re-extraction upsert.

* Values are normalised with the P1 ``normalize_answers`` per question type; an invalid value keeps the verbatim text,
  ``normalized=None``, flag ``invalid_value`` and a confidence capped at 0.3.
* ``knockout`` flags an answer meeting its knockout criterion (display only; nothing ever rejects).
* ``low_confidence`` flags answers under the tenant threshold (never auto-accepted).
* One current row per ``(transcript_id, question_id)``: a new extraction supersedes an unreviewed row; a row a person
  already decided is kept.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailed
from app.models.screening_agent import MAX_ANSWER_TEXT, ScreeningAnswer
from app.services.screening_agent.common import now
from app.services.screening_agent.questionnaires import knockout_hit

INVALID_CAP = Decimal("0.30")


def normalize(question: dict[str, Any], value: Any) -> dict[str, Any] | None:
    """``{"value": v[, "unit": u][, "currency": c]}`` or None when the value does not fit the question."""
    from app.services.pipeline.screening import normalize_answers

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    kind = question.get("type")
    raw = value
    if kind == "money" and not isinstance(value, dict):
        raw = {"amount": value, "currency": question.get("currency")}
    p1q = {"key": "q", "label": "q", "type": kind, "options": question.get("options")}
    try:
        out = normalize_answers([p1q], {"q": raw}, require=False)
    except ValidationFailed:
        return None
    if "q" not in out:
        return None
    normalized = out["q"]
    if kind in ("number", "rating"):
        lo, hi = question.get("min"), question.get("max")
        if (lo is not None and normalized < lo) or (hi is not None and normalized > hi):
            return None
    result: dict[str, Any] = {"value": normalized}
    if question.get("unit"):
        result["unit"] = question["unit"]
    return result


def flags_for(
    question: dict[str, Any], normalized: dict[str, Any] | None, confidence: Decimal, threshold: Decimal
) -> list[str]:
    flags: list[str] = []
    if normalized is None:
        flags.append("invalid_value")
    elif knockout_hit(question, normalized.get("value")):
        flags.append("knockout")
    if confidence < threshold:
        flags.append("low_confidence")
    return flags


async def current(db: AsyncSession, transcript_id: uuid.UUID) -> list[ScreeningAnswer]:
    return list(
        (
            await db.execute(
                select(ScreeningAnswer)
                .where(
                    ScreeningAnswer.transcript_id == transcript_id, ScreeningAnswer.superseded_at.is_(None)
                )
                .order_by(ScreeningAnswer.created_at)
            )
        ).scalars()
    )


async def upsert(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    application_id: uuid.UUID,
    source: str,
    conversation_id: uuid.UUID | None,
    voice_session_id: uuid.UUID | None,
    transcript_id: uuid.UUID,
    version_id: uuid.UUID,
    question: dict[str, Any],
    answer_text: str,
    normalized: dict[str, Any] | None,
    confidence: Decimal,
    segment_refs: list[int],
    start_ms: int | None,
    flags: list[str],
    ai_action_id: uuid.UUID | None,
) -> ScreeningAnswer:
    question_id = uuid.UUID(str(question["id"]))
    previous = (
        await db.execute(
            select(ScreeningAnswer)
            .where(
                ScreeningAnswer.transcript_id == transcript_id,
                ScreeningAnswer.question_id == question_id,
                ScreeningAnswer.superseded_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous is not None:
        if previous.status != "unconfirmed":
            return previous  # a person decided it: a re-extraction never overrides the review
        if (
            previous.answer_text == answer_text
            and previous.normalized == normalized
            and previous.confidence == confidence
        ):
            return previous
        previous.superseded_at = now()
        await db.flush()
    row = ScreeningAnswer(
        id=uuid.uuid4(), tenant_id=tenant_id, candidate_id=candidate_id, application_id=application_id, source=source,
        conversation_id=conversation_id, voice_session_id=voice_session_id, transcript_id=transcript_id,
        questionnaire_version_id=version_id, question_id=question_id, answer_text=answer_text[:MAX_ANSWER_TEXT] or None,
        normalized=normalized, confidence=confidence, segment_refs=segment_refs, start_ms=start_ms, flags=flags,
        status="unconfirmed", ai_action_id=ai_action_id,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    return row


__all__ = ["current", "flags_for", "normalize", "upsert"]
