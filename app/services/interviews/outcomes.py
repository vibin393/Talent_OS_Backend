"""Interview stage outcomes, selection and the application interview summary (7.3, FR-42, FR-65).

* ``record_stage_outcome`` -- ``interview:outcome``, human actors only (``ai`` / ``workflow`` / system -> 403
  ``human_only_action``), and at least one interview of that stage must have started (409
  ``outcome_before_interview``). A newer outcome supersedes the older one for the same stage.
  - ``reject`` -> ``transition_application(to="rejected", actor=user, reason_code or "interview_rejected")`` in
    the same transaction, and the application's other scheduled interviews are cancelled (with invites).
  - ``advance`` on the final stage makes the application selectable (``can_select``).
* ``select_application`` -> ``transition_application(to="selected", actor=user)``; 409
  ``final_stage_not_advanced`` unless the final stage's latest outcome is ``advance``.
* ``board_badges`` -- ``next_interview_at`` / ``scorecards_pending`` for the Epic 6 pipeline board.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.core.events import emit
from app.core.permissions import check
from app.events.interviews import InterviewStageOutcomeRecorded
from app.models.interviews import (
    Interview,
    InterviewParticipant,
    InterviewStage,
    InterviewStageOutcome,
    Scorecard,
)
from app.schemas.interviews import (
    BoardBadge,
    InterviewSummaryOut,
    RecommendationCounts,
    SelectOut,
    StageOutcomeIn,
    StageOutcomeOut,
    StageSummaryOut,
)
from app.services.interviews import plan, ports, scheduling, scorecards
from app.services.interviews.common import (
    OUTCOME,
    READ,
    actor_kind_of,
    conflict,
    now,
    principal_of,
    tenant_of,
)

HUMAN_ONLY_CODE = "human_only_action"


def _human_only(ctx: RequestContext) -> uuid.UUID:
    principal = principal_of(ctx)
    if actor_kind_of(ctx) != "user" or principal.type != "user":
        raise Forbidden("Only a person can record interview outcomes", code=HUMAN_ONLY_CODE)
    return principal.id


def _out(row: InterviewStageOutcome, can_select: bool) -> StageOutcomeOut:
    return StageOutcomeOut(
        id=row.id,
        application_id=row.application_id,
        stage_id=row.stage_id,
        outcome=row.outcome,
        reason_code=row.reason_code,
        comment=row.comment,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        superseded_at=row.superseded_at,
        can_select=can_select,
    )


async def _latest(db: AsyncSession, application_id: uuid.UUID) -> dict[uuid.UUID, InterviewStageOutcome]:
    rows = await db.execute(
        select(InterviewStageOutcome).where(
            InterviewStageOutcome.application_id == application_id,
            InterviewStageOutcome.superseded_at.is_(None),
        )
    )
    return {r.stage_id: r for r in rows.scalars()}


async def selection_eligible(db: AsyncSession, application: Any) -> bool:
    """Return whether the latest outcome of the demand plan's final stage is ``advance``."""
    stages = await plan.active_stages(db, application.demand_id)
    final = next((s for s in stages if s.is_final), None)
    if final is None:
        return False
    latest = (await _latest(db, application.id)).get(final.id)
    return latest is not None and latest.outcome == "advance"


async def record_stage_outcome(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, data: StageOutcomeIn
) -> StageOutcomeOut:
    user_id = _human_only(ctx)
    principal = principal_of(ctx)
    app = await ports.application_for(ctx, db, application_id, for_update=True)
    check(principal, OUTCOME)
    stage = await db.get(InterviewStage, data.stage_id)
    if stage is None or stage.demand_id != app.demand_id:
        raise conflict("stage_not_in_plan", "The interview stage does not belong to this demand's plan")
    at = now()
    started = (
        await db.execute(
            select(Interview.id)
            .where(
                Interview.application_id == app.id,
                Interview.stage_id == stage.id,
                Interview.status != "cancelled",
                Interview.starts_at <= at,
            )
            .limit(1)
        )
    ).first()
    if started is None:
        raise conflict(
            "outcome_before_interview",
            "An outcome can be recorded once an interview of this stage has started",
        )
    previous = (await _latest(db, app.id)).get(stage.id)
    if previous is not None:
        previous.superseded_at = at
    reason = (data.reason_code or "interview_rejected") if data.outcome == "reject" else data.reason_code
    row = InterviewStageOutcome(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        application_id=app.id,
        stage_id=stage.id,
        outcome=data.outcome,
        reason_code=reason,
        comment=data.comment,
        decided_by=user_id,
        decided_at=at,
    )
    db.add(row)
    await db.flush()
    if data.outcome == "reject":
        await ports.transition(ctx, db, app.id, "rejected", "user", reason, data.comment)
        await scheduling.cancel_open_for_application(ctx, db, app.id, "Application rejected")
    can_select = data.outcome == "advance" and stage.is_final and stage.archived_at is None
    emit(
        db,
        InterviewStageOutcomeRecorded(
            tenant_id=row.tenant_id,
            application_id=app.id,
            stage_id=stage.id,
            outcome=data.outcome,
            reason_code=reason,
            decided_by=user_id,
            can_select=can_select,
        ),
    )
    return _out(row, can_select)


async def select_application(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> SelectOut:
    _human_only(ctx)
    principal = principal_of(ctx)
    app = await ports.application_for(ctx, db, application_id, for_update=True)
    check(principal, OUTCOME)
    if not await selection_eligible(db, app):
        raise conflict("final_stage_not_advanced", "Record Advance on the final interview stage first")
    await ports.transition(ctx, db, app.id, "selected", "user")
    return SelectOut(application_id=app.id, stage_key="selected")


# ================================================================ summary
async def interview_summary(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> InterviewSummaryOut:
    principal = principal_of(ctx)
    app = await ports.application_for(ctx, db, application_id)
    check(principal, READ)
    demand = await ports.demand_row(db, app.demand_id)
    stages = await plan.ensure_plan(db, demand)
    rows = list(
        (
            await db.execute(
                select(Interview)
                .where(Interview.application_id == app.id)
                .order_by(Interview.starts_at, Interview.id)
            )
        ).scalars()
    )
    held_ids = {r.stage_id for r in rows} - {s.id for s in stages}
    if held_ids:
        archived = (await db.execute(select(InterviewStage).where(InterviewStage.id.in_(held_ids)))).scalars()
        stages = [*stages, *sorted(archived, key=lambda s: s.position)]
    outs = {o.id: o for o in await scheduling.interview_outs(db, rows)}
    visible = await scorecards.visible_for(principal, db, rows)
    cards = list(
        (
            await db.execute(
                select(Scorecard).where(Scorecard.interview_id.in_([r.id for r in rows] or [uuid.uuid4()]))
            )
        ).scalars()
    )
    latest = await _latest(db, app.id)
    stage_outs = {s.id: s for s in await plan.stage_outs(db, stages)}
    eligible = await selection_eligible(db, app)
    items: list[StageSummaryOut] = []
    for stage in stages:
        stage_rows = [r for r in rows if r.stage_id == stage.id]
        submitted = sum(o.scorecards_submitted for o in (outs[r.id] for r in stage_rows))
        required = sum(o.scorecards_required for o in (outs[r.id] for r in stage_rows))
        hidden = any(not visible.get(r.id, False) for r in stage_rows)
        recs: RecommendationCounts | None = None
        if not hidden:
            recs = RecommendationCounts()
            for c in cards:
                if (
                    c.status == "submitted"
                    and c.recommendation
                    and any(c.interview_id == r.id for r in stage_rows)
                ):
                    setattr(recs, c.recommendation, getattr(recs, c.recommendation) + 1)
        outcome = latest.get(stage.id)
        items.append(
            StageSummaryOut(
                stage=stage_outs[stage.id],
                interviews=[outs[r.id] for r in stage_rows],
                scorecards_submitted=submitted,
                scorecards_required=required,
                hidden=hidden,
                recommendations=recs,
                latest_outcome=_out(outcome, eligible and stage.is_final) if outcome else None,
            )
        )
    return InterviewSummaryOut(application_id=app.id, stages=items, can_select=eligible)


# ================================================================ pipeline board badges (Epic 6 calls this)
async def board_badges(
    ctx: RequestContext | None,  # ruff: ignore[unused-function-argument] - pipeline board port signature
    db: AsyncSession,
    application_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, BoardBadge]:
    """``{application_id: {next_interview_at, scorecards_pending}}`` (missing ids = no interviews)."""
    ids = list(dict.fromkeys(application_ids))
    if not ids:
        return {}
    at = now()
    nxt = dict(
        (
            await db.execute(
                select(Interview.application_id, func.min(Interview.starts_at))
                .where(
                    Interview.application_id.in_(ids),
                    Interview.status == "scheduled",
                    Interview.ends_at >= at,
                )
                .group_by(Interview.application_id)
            )
        )
        .tuples()
        .all()
    )
    submitted = select(Scorecard.participant_id).where(Scorecard.status == "submitted")
    pending = dict(
        (
            await db.execute(
                select(Interview.application_id, func.count(InterviewParticipant.id))
                .join(InterviewParticipant, InterviewParticipant.interview_id == Interview.id)
                .where(
                    Interview.application_id.in_(ids),
                    Interview.status.in_(("scheduled", "completed")),
                    Interview.starts_at <= at,
                    InterviewParticipant.scorecard_required.is_(True),
                    InterviewParticipant.removed_at.is_(None),
                    InterviewParticipant.id.not_in(submitted),
                )
                .group_by(Interview.application_id)
            )
        )
        .tuples()
        .all()
    )
    return {
        i: BoardBadge(next_interview_at=nxt.get(i), scorecards_pending=int(pending.get(i, 0)))
        for i in ids
        if i in nxt or i in pending
    }
