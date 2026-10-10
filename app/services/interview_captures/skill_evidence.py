# ruff: noqa: E501
"""Skill evidence suggestions from an interview (Epic 60, story 60.4; FR-303; coordinator decision C-10).

* ``suggest_from_capture`` -- each item (skill name, proficiency 1..5, quote, segment) becomes a P3 skill suggestion of
  source ``interview`` (``source_ref = interview_capture:<id>``), but only when the quote is in the referenced segment
  and the name resolves to a catalogue skill (``resolve_skill(create_pending=False)``; unknown names are skipped).
  P3 rules apply: a skill the Candidate already has, a pending suggestion, or a rejected (skill, interview) pair is
  not suggested again.
* ``verify_level`` (registered with ``skills_service.register_evidence_verifier("interview", ...)``): ``verified`` iff
  the accepting User is an internal interviewer participant of the Interview in ``source_ref`` -> evidence kind
  ``interview`` (needs ``skill:verify``, human only); anyone else -> ``interview_declared``.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal
from app.models.interview_captures import InterviewCapture
from app.models.interviews import InterviewParticipant
from app.services.interview_captures import common, validators

SOURCE = "interview"


def source_ref(capture_id: uuid.UUID) -> str:
    return f"interview_capture:{capture_id}"


async def suggest_from_capture(
    ctx: RequestContext, db: AsyncSession, capture: InterviewCapture, items: list[dict[str, Any]]
) -> tuple[int, list[dict[str, str]]]:
    from app.core.skill_refs import SkillKey
    from app.services import skills_service
    from app.services.candidates import skills as catalog

    if capture.status in ("erased", "planned", "consent_pending", "blocked") or capture.transcript_id is None:
        raise common.conflict("capture_not_ready", "There is no consented transcript for this interview")
    segs = await validators.segments(db, capture.transcript_id)
    suggested = 0
    skipped: list[dict[str, str]] = []
    for item in items:
        name = " ".join(str(item["skill_name"]).split())[:100]
        validators.check_quote(segs, int(item["segment_seq"]), str(item["quote"]))
        ref = await catalog.resolve_skill(ctx, db, name, create_pending=False)
        if ref is None:
            skipped.append({"skill_name": name, "reason": "unknown_skill"})
            continue
        created = await skills_service.suggest_skill(
            ctx, db, capture.candidate_id, SkillKey(kind=ref.kind, id=ref.id), source=SOURCE,
            proficiency=int(item["proficiency"]), rationale=str(item["quote"])[:500], source_ref=source_ref(capture.id),
        )  # fmt: skip
        if created:
            suggested += 1
        else:
            skipped.append({"skill_name": name, "reason": "already_known_or_decided"})
    return suggested, skipped


async def verify_level(ctx: RequestContext, db: AsyncSession, suggestion: Any) -> str:
    principal = ctx.principal
    ref = str(getattr(suggestion, "source_ref", "") or "")
    if (
        not isinstance(principal, Principal)
        or principal.type != "user"
        or not ref.startswith("interview_capture:")
    ):
        return "declared"
    try:
        capture_id = uuid.UUID(ref.split(":", 1)[1])
    except ValueError:
        return "declared"
    capture = await db.get(InterviewCapture, capture_id)
    if capture is None:
        return "declared"
    hit = (
        await db.execute(
            select(InterviewParticipant.id).where(
                InterviewParticipant.interview_id == capture.interview_id,
                InterviewParticipant.user_id == principal.id,
                InterviewParticipant.kind == "interviewer_internal",
                InterviewParticipant.removed_at.is_(None),
            )
        )
    ).first()
    return "verified" if hit is not None else "declared"


async def reject_pending(db: AsyncSession, capture_id: uuid.UUID) -> int:
    """Withdrawal: pending suggestions from this capture are rejected by the system (never reach the profile)."""
    from app.models.skills_graph import SkillSuggestion

    rows = list(
        (
            await db.execute(
                select(SkillSuggestion).where(
                    SkillSuggestion.source_ref == source_ref(capture_id), SkillSuggestion.status == "pending"
                )
            )
        ).scalars()
    )
    for r in rows:
        r.status = "rejected"
        r.rationale = None
        r.decided_at = common.now()
    await db.flush()
    return len(rows)


__all__ = ["SOURCE", "reject_pending", "source_ref", "suggest_from_capture", "verify_level"]
