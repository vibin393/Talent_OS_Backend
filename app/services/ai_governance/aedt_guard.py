# ruff: file-ignore[line-too-long]
"""An alternative-process request stops agent screening (Epic 65 story 65.6, FR-323, P3 FR-249; C-18).

* ``on_alternative_requested`` -- ``AlternativeRequested``: cancel the Application's active ``screening`` /
  ``voice_screening`` runs (reason ``alternative_process``), pause its open screening conversation (57's exported
  ``pause``) and add a timeline entry "AI screening stopped: the candidate asked for an alternative process".
* ``on_run_started`` -- ``AgentRunStarted`` of a screening / voice run on an Application with an ``open`` or
  ``handled`` request: cancel it before step 1 (the dispatch race; the step task re-checks the status).
"""

import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.agentic import RunQuery
from app.services import agent_runtime_service as runtime

SCREENING_AGENTS = ("screening", "voice_screening")
NOTE = "AI screening stopped: the candidate asked for an alternative process"


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def _cancel_active(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> int:
    from app.models.agent_runtime import AgentRun

    rows = (
        await db.execute(
            select(AgentRun.id).where(
                AgentRun.subject_type == "application", AgentRun.subject_id == application_id,
                AgentRun.agent_key.in_(SCREENING_AGENTS), AgentRun.status.in_(("queued", "running", "waiting")),
            )
        )
    ).scalars()  # fmt: skip
    n = 0
    for run_id in list(rows):
        await runtime.cancel_run(
            ctx, db, run_id, reason="alternative_process", reason_code="alternative_process"
        )
        n += 1
    return n


async def on_alternative_requested(db: AsyncSession, tenant_id: uuid.UUID, request_id: uuid.UUID) -> int:
    from app.services.fairness import aedt_p4

    found = await aedt_p4.request_application(db, request_id)
    if found is None:
        return 0
    application_id, _status = found
    ctx = _ctx(tenant_id)
    cancelled = await _cancel_active(ctx, db, application_id)
    paused = False
    try:
        from app.models.screening_agent import ScreeningConversation
        from app.services import screening_agent_service

        conv_ids = (
            await db.execute(
                select(ScreeningConversation.id).where(
                    ScreeningConversation.application_id == application_id,
                    ScreeningConversation.status.in_(("invited", "consented", "in_progress")),
                )
            )
        ).scalars()
        for conv_id in list(conv_ids):
            async with db.begin_nested():
                await screening_agent_service.pause(ctx, db, conv_id, reason="alternative_process")
            paused = True
    except ImportError:  # pragma: no cover - the Screening Agent is not installed
        logger.debug("Screening Agent not installed; nothing to pause")
    if cancelled or paused:
        from app.services import activity_service

        await activity_service.add_system_entry(ctx, db, "application", application_id, NOTE)
    await db.flush()
    return cancelled + int(paused)


async def on_run_started(db: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    from app.models.agent_runtime import AgentRun
    from app.services.fairness import aedt_p4

    run = await db.get(AgentRun, run_id)
    if (
        run is None or run.agent_key not in SCREENING_AGENTS or run.subject_type != "application"
        or run.subject_id is None or run.status not in ("queued", "running", "waiting")
    ):  # fmt: skip
        return False
    if not await aedt_p4.open_request_for(db, run.subject_id):
        return False
    await runtime.cancel_run(
        _ctx(tenant_id), db, run.id, reason="alternative_process", reason_code="alternative_process"
    )
    return True


async def active_screening_runs(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> int:
    """Count helper for tests and the console (foundation list)."""
    total = 0
    for key in SCREENING_AGENTS:
        q = RunQuery(agent_key=key, subject_type="application", subject_id=application_id, page_size=100)
        total += sum(
            1
            for r in (await runtime.list_runs(ctx, db, q)).items
            if r.status in ("queued", "running", "waiting")
        )
    return total


__all__ = ["NOTE", "SCREENING_AGENTS", "active_screening_runs", "on_alternative_requested", "on_run_started"]
