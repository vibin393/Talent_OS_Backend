"""Scorecards (7.3, FR-41, NFR-17).

* Submission needs every kit competency rated ``1..rating_scale`` and a recommendation (422
  ``scorecard_incomplete``); a submitted scorecard is immutable (409 ``scorecard_locked``); submitting needs
  ``starts_at <= now`` (409 ``interview_not_started``) and a non-cancelled interview (409
  ``interview_cancelled``). Scorecards snapshot the interview's kit.
* **Blind rule** (enforced here, never only in the UI): other interviewers' scorecards are returned only when
  the caller's own scorecard for the interview is submitted, or when the caller holds ``interview:outcome``
  and is not a participant with ``scorecard_required``. Otherwise ``{hidden: true, submitted_count,
  required_count}``. The application-level aggregate (``visible_for``) applies the same rule.
* Submitting in the app is human-only (FR-325 ★): an automated actor (agent, AI, workflow, system,
  API key) gets 403 ``human_only_action`` before anything is read.
* External interviewers submit through their secure link (purpose ``interview_scorecard``); they never see
  other scorecards.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import Principal
from app.events.interviews import ScorecardSubmitted
from app.models.interviews import Interview, InterviewParticipant, Scorecard
from app.schemas.interviews import (
    AnswerIn,
    PublicCompetency,
    PublicKitQuestion,
    PublicScorecardIn,
    PublicScorecardView,
    RatingIn,
    ScorecardDraftIn,
    ScorecardListOut,
    ScorecardOut,
)
from app.services.interviews import ports, timeutil
from app.services.interviews.common import (
    OUTCOME,
    RECOMMENDATION_OPTIONS,
    acting_as,
    conflict,
    now,
    principal_of,
)
from app.services.interviews.scheduling import (
    PARTICIPANT_SUBJECT,
    SCORECARD_PURPOSE,
    interview_for,
    participants_of,
)


def _kit(row: Interview) -> dict[str, Any]:
    kit = dict(row.kit_snapshot or {})
    if not kit.get("competencies"):
        kit["competencies"] = [{"key": "overall", "label": "Overall", "guidance": None}]
    kit.setdefault("questions", [])
    kit.setdefault("rating_scale", 5)
    kit.setdefault("recommendation_options", list(RECOMMENDATION_OPTIONS))
    return kit


def _out(row: Interview, p: InterviewParticipant, card: Scorecard | None) -> ScorecardOut:
    return ScorecardOut(
        id=card.id if card else None,
        interview_id=row.id,
        participant_id=p.id,
        participant_name=p.name,
        participant_kind=p.kind,
        author_user_id=card.author_user_id if card else None,
        kit=(card.kit_snapshot if card and card.kit_snapshot else _kit(row)),
        ratings=[RatingIn.model_validate(r) for r in (card.ratings if card else [])],
        answers=[AnswerIn.model_validate(a) for a in (card.answers if card else [])],
        recommendation=card.recommendation if card else None,
        summary=card.summary if card else None,
        status=card.status if card else "draft",
        submitted_at=card.submitted_at if card else None,
        source=card.source if card else "in_app",
    )


def _validate(kit: dict[str, Any], data: ScorecardDraftIn | PublicScorecardIn, *, final: bool) -> None:
    scale = int(kit.get("rating_scale") or 5)
    competencies = [c["key"] for c in kit.get("competencies") or []]
    questions = {q["key"] for q in kit.get("questions") or []}
    problems: dict[str, Any] = {}
    seen: set[str] = set()
    for r in data.ratings:
        if r.competency_key not in competencies:
            problems.setdefault("unknown_competencies", []).append(r.competency_key)
        elif r.competency_key in seen:
            problems.setdefault("duplicate_competencies", []).append(r.competency_key)
        seen.add(r.competency_key)
        if not 1 <= r.rating <= scale:
            problems.setdefault("out_of_range", []).append(r.competency_key)
    for a in data.answers:
        if a.question_key not in questions:
            problems.setdefault("unknown_questions", []).append(a.question_key)
    if final:
        missing = [c for c in competencies if c not in seen]
        if missing:
            problems["missing_ratings"] = missing
        if data.recommendation is None:
            problems["recommendation"] = "required"
    if problems:
        raise ValidationFailed(
            f"Rate every competency (1-{scale}) and choose a recommendation"
            if final
            else "Invalid scorecard",
            code="scorecard_incomplete" if final else "invalid_scorecard",
            details=problems,
        )


async def _card(
    db: AsyncSession, interview_id: uuid.UUID, participant_id: uuid.UUID, *, for_update: bool = False
) -> Scorecard | None:
    stmt = select(Scorecard).where(
        Scorecard.interview_id == interview_id, Scorecard.participant_id == participant_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _my_participant(
    db: AsyncSession, row: Interview, user_id: uuid.UUID
) -> InterviewParticipant | None:
    return (
        await db.execute(
            select(InterviewParticipant).where(
                InterviewParticipant.interview_id == row.id,
                InterviewParticipant.user_id == user_id,
                InterviewParticipant.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _mine(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, *, for_update: bool = False
) -> tuple[Interview, InterviewParticipant]:
    principal = principal_of(ctx)
    row = await interview_for(ctx, db, interview_id, for_update=for_update)
    p = await _my_participant(db, row, principal.id)
    if p is None or p.kind not in ("interviewer_internal", "observer"):
        raise Forbidden("Only the interview's interviewers have a scorecard", code="not_an_interviewer")
    return row, p


def _apply(card: Scorecard, data: ScorecardDraftIn | PublicScorecardIn) -> None:
    card.ratings = [r.model_dump() for r in data.ratings]
    card.answers = [a.model_dump() for a in data.answers]
    card.recommendation = data.recommendation
    card.summary = data.summary


def _new_card(row: Interview, p: InterviewParticipant, author: uuid.UUID | None, source: str) -> Scorecard:
    return Scorecard(
        id=uuid.uuid4(),
        tenant_id=row.tenant_id,
        interview_id=row.id,
        participant_id=p.id,
        author_user_id=author,
        kit_snapshot=_kit(row),
        ratings=[],
        answers=[],
        status="draft",
        source=source,
    )


def _can_submit(row: Interview) -> None:
    if row.status == "cancelled":
        raise conflict("interview_cancelled", "The interview was cancelled")
    if row.starts_at > now():
        raise conflict("interview_not_started", "Scorecards can be submitted once the interview has started")


# ================================================================ in-app (internal interviewers)
async def get_mine(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> ScorecardOut:
    row, p = await _mine(ctx, db, interview_id)
    return _out(row, p, await _card(db, row.id, p.id))


async def save_draft(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, data: ScorecardDraftIn
) -> ScorecardOut:
    row, p = await _mine(ctx, db, interview_id)
    if row.status == "cancelled":
        raise conflict("interview_cancelled", "The interview was cancelled")
    card = await _card(db, row.id, p.id, for_update=True)
    if card is not None and card.status == "submitted":
        raise conflict("scorecard_locked", "A submitted scorecard cannot be changed")
    _validate(card.kit_snapshot if card and card.kit_snapshot else _kit(row), data, final=False)
    if card is None:
        card = _new_card(row, p, p.user_id, "in_app")
        db.add(card)
    _apply(card, data)
    await db.flush()
    return _out(row, p, card)


async def submit_mine(
    ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID, data: ScorecardDraftIn | None
) -> ScorecardOut:
    require_human(actor_from_ctx(ctx))  # FR-325 ★: only a person submits a scorecard (403 human_only_action)
    row, p = await _mine(ctx, db, interview_id)
    card = await _card(db, row.id, p.id, for_update=True)
    if card is not None and card.status == "submitted":
        raise conflict("scorecard_locked", "A submitted scorecard cannot be changed")
    _can_submit(row)
    if data is None:
        if card is None:
            data = ScorecardDraftIn()
        else:
            data = ScorecardDraftIn(
                ratings=[RatingIn.model_validate(r) for r in card.ratings or []],
                answers=[AnswerIn.model_validate(a) for a in card.answers or []],
                recommendation=card.recommendation,
                summary=card.summary,
            )
    kit = card.kit_snapshot if card and card.kit_snapshot else _kit(row)
    _validate(kit, data, final=True)
    if card is None:
        card = _new_card(row, p, p.user_id, "in_app")
        db.add(card)
    _apply(card, data)
    card.status = "submitted"
    card.submitted_at = now()
    card.source = "in_app"
    card.author_user_id = p.user_id
    await db.flush()
    emit(
        db,
        ScorecardSubmitted(
            tenant_id=row.tenant_id,
            interview_id=row.id,
            application_id=row.application_id,
            scorecard_id=card.id,
            participant_id=p.id,
            source="in_app",
            author_id=p.user_id,
        ),
    )
    return _out(row, p, card)


# ================================================================ blind rule
def _may_see_all(principal: Principal, me: InterviewParticipant | None, my_card: Scorecard | None) -> bool:
    if me is not None and my_card is not None and my_card.status == "submitted":
        return True
    return OUTCOME in principal.permissions and not (me is not None and me.scorecard_required)


async def visible_for(
    principal: Principal, db: AsyncSession, interviews: Sequence[Interview]
) -> dict[uuid.UUID, bool]:
    """interview_id -> may ``principal`` see the scorecards of that interview."""
    ids = [r.id for r in interviews]
    if not ids:
        return {}
    mine = {
        p.interview_id: p
        for p in (
            await db.execute(
                select(InterviewParticipant).where(
                    InterviewParticipant.interview_id.in_(ids),
                    InterviewParticipant.user_id == principal.id,
                    InterviewParticipant.removed_at.is_(None),
                )
            )
        ).scalars()
    }
    cards = {
        c.participant_id: c
        for c in (
            await db.execute(
                select(Scorecard).where(
                    Scorecard.participant_id.in_([p.id for p in mine.values()] or [uuid.uuid4()])
                )
            )
        ).scalars()
    }
    return {
        i: _may_see_all(principal, mine.get(i), cards.get(mine[i].id) if i in mine else None) for i in ids
    }


async def list_scorecards(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> ScorecardListOut:
    principal = principal_of(ctx)
    row = await interview_for(ctx, db, interview_id)
    parts = (await participants_of(db, [row.id])).get(row.id, [])
    cards = {
        c.participant_id: c
        for c in (await db.execute(select(Scorecard).where(Scorecard.interview_id == row.id))).scalars()
    }
    required = [p for p in parts if p.scorecard_required]
    submitted = sum(1 for p in required if cards.get(p.id) is not None and cards[p.id].status == "submitted")
    me = next((p for p in parts if p.user_id == principal.id), None)
    my_card = cards.get(me.id) if me is not None else None
    if not _may_see_all(principal, me, my_card):
        own = [_out(row, me, my_card)] if me is not None and my_card is not None else []
        return ScorecardListOut(
            hidden=True, submitted_count=submitted, required_count=len(required), items=own
        )
    items = [_out(row, p, cards[p.id]) for p in parts if p.id in cards and cards[p.id].status == "submitted"]
    if me is not None and my_card is not None and my_card.status != "submitted":
        items.append(_out(row, me, my_card))
    return ScorecardListOut(
        hidden=False, submitted_count=submitted, required_count=len(required), items=items
    )


# ================================================================ external (secure link)
def _display_name(full_name: str | None) -> str:
    parts = (full_name or "").split()
    if not parts:
        return "Candidate"
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]


async def _link_subject(
    db: AsyncSession, link: secure_links.ResolvedLink, *, for_update: bool = False
) -> tuple[Interview, InterviewParticipant]:
    if link.subject_type != PARTICIPANT_SUBJECT:
        raise NotFound("Link not found", code="link_invalid")
    stmt = select(InterviewParticipant).where(InterviewParticipant.id == link.subject_id)
    if for_update:
        stmt = stmt.with_for_update()
    p = (await db.execute(stmt)).scalar_one_or_none()
    if p is None or p.removed_at is not None or p.kind != "interviewer_external":
        raise NotFound("Link not found", code="link_invalid")
    row = await db.get(Interview, p.interview_id)
    if row is None:
        raise NotFound("Link not found", code="link_invalid")
    return row, p


async def public_view(link: secure_links.ResolvedLink) -> PublicScorecardView:
    async with tenant_session(link.tenant_id) as db:
        row, p = await _link_subject(db, link)
        card = await _card(db, row.id, p.id)
        candidate = await ports.candidate_summary(db, row.candidate_id)
        titles = await ports.demand_titles(db, [row.demand_id])
        kit = _kit(row)
        state = (
            "submitted"
            if card is not None and card.status == "submitted"
            else ("not_started" if row.starts_at > now() else "open")
        )
        return PublicScorecardView(
            state=state,
            candidate_display_name=_display_name(candidate.get("full_name")),
            demand_title=titles.get(row.demand_id),
            stage_name=row.stage_name,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            timezone=p.timezone,
            display_time=timeutil.render_for(row.starts_at, p.timezone),
            rating_scale=int(kit.get("rating_scale") or 5),
            recommendation_options=list(kit.get("recommendation_options") or RECOMMENDATION_OPTIONS),
            competencies=[
                PublicCompetency(key=c["key"], label=c["label"], guidance=c.get("guidance"))
                for c in kit["competencies"]
            ],
            questions=[
                PublicKitQuestion(
                    key=q["key"],
                    competency_key=q.get("competency_key"),
                    text=q["text"],
                    guidance=q.get("guidance"),
                )
                for q in kit["questions"]
            ],
        )


async def public_submit(link: secure_links.ResolvedLink, data: PublicScorecardIn) -> str:
    """Validate first (a refused submission keeps the link usable), then consume the link and store."""
    async with tenant_session(link.tenant_id) as db, _system(db):
        row, p = await _link_subject(db, link, for_update=True)
        card = await _card(db, row.id, p.id, for_update=True)
        if card is not None and card.status == "submitted":
            raise conflict("scorecard_locked", "This scorecard was already submitted")
        _can_submit(row)
        _validate(card.kit_snapshot if card and card.kit_snapshot else _kit(row), data, final=True)
        await secure_links.consume(db, link.id)
        if card is None:
            card = _new_card(row, p, None, "secure_link")
            db.add(card)
        _apply(card, data)
        card.status = "submitted"
        card.submitted_at = now()
        card.source = "secure_link"
        card.secure_link_id = link.id
        await db.flush()
        emit(
            db,
            ScorecardSubmitted(
                tenant_id=row.tenant_id,
                interview_id=row.id,
                application_id=row.application_id,
                scorecard_id=card.id,
                participant_id=p.id,
                source="secure_link",
            ),
        )
    return "submitted"


class _system:  # ruff: ignore[invalid-class-name] - tiny context manager
    """Attribute audit rows of a public (no principal) action to ``secure_link``."""

    def __init__(self, db: AsyncSession) -> None:
        self._cm = acting_as(db, "secure_link") if _secure_link_actor_ok() else acting_as(db, "system")

    async def __aenter__(self) -> None:
        self._cm.__enter__()

    async def __aexit__(self, *exc: object) -> None:
        self._cm.__exit__(*exc)


def _secure_link_actor_ok() -> bool:
    from app.core import audit

    return "secure_link" in getattr(audit, "ACTOR_TYPES", ())


__all__ = [
    "SCORECARD_PURPOSE",
    "get_mine",
    "list_scorecards",
    "public_submit",
    "public_view",
    "save_draft",
    "submit_mine",
    "visible_for",
]
