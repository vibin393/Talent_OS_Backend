"""Every call the interviews domain makes into other domains, in one place (AD-1: through their services).

* pipeline (Epic 6): application visibility / rows, the current stage key, ``transition_application``.
* demands (Epic 4): visibility, rows, titles, the legacy ``interview_plan`` seed, progress counters, the
  ``first_interview`` SLA clock.
* candidates (Epic 3): names and primary email.
* directory (Epic 1 / 10): user names + emails, tenant name + timezone.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services import entity_refs


# ================================================================ pipeline
async def application_for(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    permission: str = "application:read",
    *,
    for_update: bool = False,
) -> Any:
    """Return the application if the caller may read it (404) and holds ``permission`` on it (403)."""
    from app.services import pipeline_service

    return await pipeline_service.applications.application_for(
        ctx, db, application_id, permission, for_update=for_update
    )


async def application_row(db: AsyncSession, application_id: uuid.UUID) -> Any:
    from app.models.pipeline import Application

    return await db.get(Application, application_id)


async def stage_key(db: AsyncSession, stage_id: uuid.UUID) -> str | None:
    from app.services import pipeline_service

    stage = await pipeline_service.stages.get_stage(db, stage_id)
    return stage.key if stage is not None else None


async def transition(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    to_key: str,
    actor: str,
    reason_code: str | None = None,
    note: str | None = None,
) -> Any:
    from app.services import pipeline_service

    return await pipeline_service.transition_application(
        ctx, db, application_id, to_key, actor, reason_code, note
    )


# ================================================================ demands
async def demand_visible(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.services import demands

    return await demands.get_for(ctx, db, demand_id, demands.READ, for_update=False)


async def demand_row(db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.services.demands import pipeline_port

    return await pipeline_port.get_row(db, demand_id)


async def demand_titles(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.services.demands import pipeline_port

    return {k: v["title"] for k, v in (await pipeline_port.titles(db, ids)).items()}


async def bump_interviews_scheduled(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, delta: int
) -> None:
    from app.services import demands

    row = await demand_row(db, demand_id)
    await demands.record_pipeline_counts(
        ctx, db, demand_id, interviews_scheduled=int(row.interviews_scheduled_count or 0) + delta
    )


async def first_interview_scheduled(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> None:
    """Stop the demand's ``first_interview`` SLA clock (no-op when absent or already stopped)."""
    from app.services import demands

    try:
        async with db.begin_nested():
            await demands.complete_sla_clock(ctx, db, demand_id, "first_interview")
    except Exception:
        from loguru import logger

        logger.exception("first_interview SLA clock could not be completed (demand={})", demand_id)


# ================================================================ candidates
async def candidate_summary(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, Any]:
    from app.services.candidates import pipeline_port

    return (await pipeline_port.summaries(db, [candidate_id])).get(candidate_id, {})


async def candidate_names(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.services.candidates import pipeline_port

    return {k: v.get("full_name") or "" for k, v in (await pipeline_port.summaries(db, ids)).items()}


async def candidate_email(db: AsyncSession, candidate_id: uuid.UUID) -> str | None:
    from app.services.candidates import pipeline_port

    email = await pipeline_port.primary_email(db, candidate_id)
    return email.lower() if email else None


# ================================================================ directory
async def users(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, entity_refs.UserInfo]:
    return await entity_refs.users(db, ids)


async def tenant(db: AsyncSession, tenant_id: uuid.UUID) -> entity_refs.TenantInfo:
    return await entity_refs.tenant(db, tenant_id)
