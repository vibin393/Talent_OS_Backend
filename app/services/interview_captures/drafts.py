# ruff: file-ignore[line-too-long]
"""Draft scorecards (Epic 60, story 60.5; FR-304, NFR-52).

* ``save`` -- one private draft per internal interviewer who must score (``scorecard_required``): one suggested rating
  per Kit competency within the Kit's ``rating_scale``, each with a rationale and transcript evidence (quotes are
  validated). There is no recommendation field: the column is always NULL (DB CHECK). The P1 ``scorecards`` table is
  never written -- the interviewer applies suggestions into their own P1 draft and submits it themselves (★).
* ``mine`` / ``mark_opened`` / ``dismiss`` -- only the interviewer the draft belongs to (anyone else -> 404).
* ``on_scorecard_submitted`` -- compares the submitted P1 ratings with the draft: ``accepted`` (equal), ``changed``
  (different), ``ignored`` (the suggestions were never opened); writes ``scorecard_suggestion_decisions``, marks the
  draft ``applied`` and emits ``DraftRatingsDecided`` (FR-323 selection data).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.interview_assistant import DraftRatingsDecided
from app.models.interview_captures import (
    InterviewCapture,
    InterviewScorecardDraft,
    ScorecardSuggestionDecision,
)
from app.models.interviews import InterviewParticipant, Scorecard
from app.schemas.interview_captures import DraftAnswerOut, DraftOut, DraftRatingOut, EvidenceQuote
from app.services.interview_captures import common, validators
from app.services.interview_captures.common import now


async def save(
    db: AsyncSession,
    capture: InterviewCapture,
    *,
    participant_id: uuid.UUID,
    ratings: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    run_id: uuid.UUID | None,
) -> InterviewScorecardDraft:
    if capture.status in ("erased", "planned", "consent_pending", "blocked") or capture.transcript_id is None:
        raise common.conflict("capture_not_ready", "There is no consented transcript for this interview")
    p = await db.get(InterviewParticipant, participant_id)
    if (
        p is None or p.interview_id != capture.interview_id or p.removed_at is not None or p.user_id is None
        or p.kind != "interviewer_internal" or not p.scorecard_required
    ):  # fmt: skip
        raise NotFound("Interviewer not found")
    interview = await common.interview_row(db, capture.interview_id)
    kit = common.kit_of(interview)
    keys = set(kit.competency_keys)
    qkeys = set(kit.question_keys)
    segs = await validators.segments(db, capture.transcript_id)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in ratings:
        key = str(r["competency_key"])
        if key not in keys:
            raise common.invalid(
                "unknown_competency",
                "Use the Kit's competency keys",
                competency_key=key,
                allowed=sorted(keys),
            )
        if key in seen:
            raise common.invalid(
                "duplicate_competency", "One suggested rating per competency", competency_key=key
            )
        seen.add(key)
        rating = int(r["suggested_rating"])
        if not 1 <= rating <= kit.rating_scale:
            raise common.invalid(
                "rating_out_of_scale", f"Ratings run from 1 to {kit.rating_scale}", competency_key=key
            )
        validators.check_language(r.get("rationale"))
        evidence = []
        for ev in r.get("evidence") or []:
            seg = validators.check_quote(segs, int(ev["segment_seq"]), str(ev["quote"]))
            evidence.append(
                {"segment_seq": seg.seq, "start_ms": seg.start_ms, "quote": str(ev["quote"])[:300]}
            )
        if not evidence:
            raise common.invalid(
                "invalid_evidence",
                "Each suggested rating needs at least one quote from the transcript",
                competency_key=key,
            )
        rows.append(
            {
                "competency_key": key,
                "suggested_rating": rating,
                "rationale": str(r.get("rationale") or "")[:600],
                "evidence": evidence,
            }
        )
    answer_rows: list[dict[str, Any]] = []
    for a in answers:
        qkey = str(a["question_key"])
        if qkey not in qkeys:
            raise common.invalid("unknown_question", "Use the Kit's question keys", question_key=qkey)
        validators.check_language(a.get("note_suggestion"))
        answer_rows.append(
            {"question_key": qkey, "note_suggestion": str(a.get("note_suggestion") or "")[:600]}
        )
    draft = (
        await db.execute(
            select(InterviewScorecardDraft)
            .where(
                InterviewScorecardDraft.capture_id == capture.id,
                InterviewScorecardDraft.participant_id == p.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if draft is not None and draft.status == "applied":
        raise common.conflict("draft_applied", "The interviewer already submitted their scorecard")
    if draft is None:
        draft = InterviewScorecardDraft(
            id=uuid.uuid4(), tenant_id=capture.tenant_id, capture_id=capture.id, interview_id=capture.interview_id,
            participant_id=p.id, user_id=p.user_id, ratings=rows, answers=answer_rows, status="ready", run_id=run_id,
        )  # fmt: skip
        db.add(draft)
    else:
        draft.ratings, draft.answers, draft.status, draft.run_id = rows, answer_rows, "ready", run_id
        draft.opened_at = None
    draft.recommendation = None
    await db.flush()
    audit.record(
        "interview_capture.draft_saved", "interview", capture.interview_id, session=db, draft_id=str(draft.id)
    )
    from app.services.interview_captures.consent import _fan_out

    _fan_out(capture.tenant_id, "scorecard_draft_ready", {
        "event_id": f"ia_draft:{draft.id}:{run_id or 'manual'}", "interview_id": str(capture.interview_id),
        "candidate_id": str(capture.candidate_id), "user_id": str(p.user_id),
    })  # fmt: skip
    return draft


def _rating_out(r: dict[str, Any], labels: dict[str, str]) -> DraftRatingOut:
    return DraftRatingOut(
        competency_key=r["competency_key"], label=labels.get(r["competency_key"]), suggested_rating=int(r["suggested_rating"]),
        rationale=r.get("rationale") or "", evidence=[EvidenceQuote(**e) for e in r.get("evidence") or []],
    )  # fmt: skip


async def draft_out(db: AsyncSession, draft: InterviewScorecardDraft) -> DraftOut:
    interview = await common.interview_row(db, draft.interview_id)
    kit = common.kit_of(interview)
    labels = {c["key"]: c["label"] for c in kit.competencies}
    settings = await common.settings_row(db)
    return DraftOut(
        id=draft.id, interview_id=draft.interview_id, participant_id=draft.participant_id,
        ratings=[_rating_out(r, labels) for r in draft.ratings or []],
        answers=[DraftAnswerOut(**a) for a in draft.answers or []], status=draft.status, rating_scale=kit.rating_scale,
        show_suggestions_by_default=settings.show_suggestions_by_default, opened_at=draft.opened_at,
    )  # fmt: skip


async def _mine(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, *, lock: bool = False
) -> InterviewScorecardDraft:
    from app.services.interview_captures import consent

    await consent.readable_interview(ctx, db, interview_id)
    user_id = common.principal_of(ctx).id
    stmt = select(InterviewScorecardDraft).where(
        InterviewScorecardDraft.interview_id == interview_id,
        InterviewScorecardDraft.user_id == user_id,
        InterviewScorecardDraft.status != "superseded",
    )
    if lock:
        stmt = stmt.with_for_update()
    draft = (
        await db.execute(stmt.order_by(InterviewScorecardDraft.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if draft is None:
        raise NotFound("Draft scorecard not found")
    return draft


async def mine(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> DraftOut:
    return await draft_out(db, await _mine(ctx, db, interview_id))


async def mark_opened(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> DraftOut:
    draft = await _mine(ctx, db, interview_id, lock=True)
    if draft.opened_at is None:
        draft.opened_at = now()
    if draft.status in ("ready", "dismissed"):
        draft.status = "opened"
    await db.flush()
    return await draft_out(db, draft)


async def dismiss(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> DraftOut:
    draft = await _mine(ctx, db, interview_id, lock=True)
    if draft.status != "applied":
        draft.status = "dismissed"
    await db.flush()
    return await draft_out(db, draft)


async def on_scorecard_submitted(db: AsyncSession, scorecard_id: uuid.UUID) -> int:
    """Record the decisions for the submitting interviewer's draft (if any). Idempotent per draft."""
    card = await db.get(Scorecard, scorecard_id)
    if card is None or card.status != "submitted":
        return 0
    draft = (
        await db.execute(
            select(InterviewScorecardDraft)
            .where(
                InterviewScorecardDraft.interview_id == card.interview_id,
                InterviewScorecardDraft.participant_id == card.participant_id,
                InterviewScorecardDraft.status.in_(("ready", "opened", "dismissed")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None:
        return 0
    final = {str(r.get("competency_key")): r.get("rating") for r in card.ratings or []}
    counts = {"accepted": 0, "changed": 0, "ignored": 0}
    at = now()
    for r in draft.ratings or []:
        key = str(r["competency_key"])
        suggested = int(r["suggested_rating"])
        got = final.get(key)
        decision = (
            "ignored"
            if draft.opened_at is None or got is None
            else ("accepted" if int(got) == suggested else "changed")
        )
        counts[decision] += 1
        db.add(
            ScorecardSuggestionDecision(
                id=uuid.uuid4(), tenant_id=draft.tenant_id, draft_id=draft.id, competency_key=key[:80],
                suggested_rating=suggested, final_rating=int(got) if got is not None else None, decision=decision,
                decided_at=at,
            )
        )  # fmt: skip
    draft.status, draft.applied_at = "applied", at
    await db.flush()
    capture = await db.get(InterviewCapture, draft.capture_id)
    emit(
        db,
        DraftRatingsDecided(
            tenant_id=draft.tenant_id, draft_id=draft.id, interview_id=draft.interview_id,
            candidate_id=capture.candidate_id if capture else card.interview_id, **counts,
        ),
    )  # fmt: skip
    audit.record(
        "interview_capture.draft_decided",
        "interview",
        draft.interview_id,
        session=db,
        draft_id=str(draft.id),
        **counts,
    )
    return sum(counts.values())


__all__ = ["dismiss", "draft_out", "mark_opened", "mine", "on_scorecard_submitted", "save"]
