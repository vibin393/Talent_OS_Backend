"""Pipeline facts and writes the portal domain (Epic 9) needs, exposed by the pipeline domain (AD-1).

* ``create_candidate_application`` -- a career-portal application with the ``candidate`` actor (the §5.2
  ``START -> applied`` row allows it); idempotency is the caller's ``existing_application`` check.
* Read models for the candidate's own applications, stage history and AI screenings. They return raw stage
  keys / kinds: the portal maps them to candidate-facing labels and never returns them.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.pipeline import (
    Application,
    ApplicationStageHistory,
    PipelineReasonCode,
    PipelineStage,
    Screening,
)
from app.services.pipeline import applications, stages


async def existing_application(
    db: AsyncSession, demand_id: uuid.UUID, candidate_id: uuid.UUID
) -> uuid.UUID | None:
    return await applications._existing(db, demand_id, candidate_id)


async def create_candidate_application(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    source: str,
    source_ref: str | None,
) -> uuid.UUID:
    from app.services.demands import pipeline_port as demands_port

    demand = await demands_port.get_row(db, demand_id)
    demands_port.assert_demand_accepts_applications(demand)
    app = await applications.create_application(
        ctx,
        db,
        demand.id,
        candidate_id,
        source=source,
        source_ref=source_ref,
        actor="candidate",
        owner_id=demand.owner_id,
    )
    await applications._sync_demand_counts(ctx, db, demand.id)
    return app.id


async def candidate_applications(db: AsyncSession, candidate_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return the candidate's applications (newest first) with their current stage key / kind."""
    await stages.ensure_default_stages(db)
    rows = (
        await db.execute(
            select(Application, PipelineStage)
            .join(PipelineStage, PipelineStage.id == Application.stage_id)
            .where(Application.candidate_id == candidate_id, Application.archived_at.is_(None))
            .order_by(Application.created_at.desc(), Application.id)
        )
    ).all()
    return [
        {
            "id": a.id,
            "demand_id": a.demand_id,
            "source_ref": a.source_ref,
            "stage_key": s.key,
            "stage_kind": s.kind,
            "terminal": stages.is_terminal(s.key),
            "applied_at": a.created_at,
            "updated_at": max(a.stage_entered_at, a.updated_at),
        }
        for a, s in rows
    ]


async def candidate_application(
    db: AsyncSession, candidate_id: uuid.UUID, application_id: uuid.UUID
) -> dict[str, Any]:
    for row in await candidate_applications(db, candidate_id):
        if row["id"] == application_id:
            return row
    raise NotFound("Application not found")


async def stage_history(db: AsyncSession, application_id: uuid.UUID) -> list[dict[str, Any]]:
    """``[{key, kind, at}]`` oldest first (target stage of every move)."""
    rows = (
        await db.execute(
            select(PipelineStage.key, PipelineStage.kind, ApplicationStageHistory.at)
            .join(PipelineStage, PipelineStage.id == ApplicationStageHistory.to_stage_id)
            .where(ApplicationStageHistory.application_id == application_id)
            .order_by(ApplicationStageHistory.at, ApplicationStageHistory.id)
        )
    ).all()
    return [{"key": k, "kind": kind, "at": at} for k, kind, at in rows]


async def withdraw_reasons(db: AsyncSession) -> list[dict[str, str]]:
    await stages.ensure_default_stages(db)
    rows = await db.execute(
        select(PipelineReasonCode.code, PipelineReasonCode.label)
        .where(PipelineReasonCode.kind == "withdraw", PipelineReasonCode.archived_at.is_(None))
        .order_by(PipelineReasonCode.label)
    )
    return [{"code": c, "label": label} for c, label in rows.all()]


async def candidate_screenings(
    db: AsyncSession, candidate_id: uuid.UUID, statuses: tuple[str, ...] | None = ("sent",)
) -> list[Screening]:
    """AI screenings of the candidate (``kind='ai'``), newest first."""
    stmt = select(Screening).where(Screening.candidate_id == candidate_id, Screening.kind == "ai")
    if statuses is not None:
        stmt = stmt.where(Screening.status.in_(statuses))
    return list((await db.execute(stmt.order_by(Screening.created_at.desc()))).scalars())


async def candidate_screening(
    db: AsyncSession, candidate_id: uuid.UUID, screening_id: uuid.UUID, *, for_update: bool = False
) -> Screening:
    row = await db.get(Screening, screening_id, with_for_update=for_update)
    if row is None or row.candidate_id != candidate_id or row.kind != "ai":
        raise NotFound("Screening not found")
    return row


async def application_demand(db: AsyncSession, application_id: uuid.UUID) -> uuid.UUID | None:
    app = await db.get(Application, application_id)
    return app.demand_id if app is not None else None


async def application_candidate(db: AsyncSession, application_id: uuid.UUID) -> uuid.UUID | None:
    app = await db.get(Application, application_id)
    return app.candidate_id if app is not None else None
