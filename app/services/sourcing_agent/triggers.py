# ruff: noqa: E501
"""Sourcing triggers and run follow-ups (Epic 56 stories 56.1 / 56.5).

* ``demand_opened`` -- a runtime event trigger on the P1 ``RediscoveryCompleted`` (declared on the AgentSpec).
* ``requirements_changed`` -- ``on_match_run``: a completed P1 match run with ``trigger='requirements_changed'`` for
  an open Demand emits ``SourcingRerankRequested`` (the runtime event trigger; dedup
  ``sourcing:req:<demand>:<match run>``, ``attach``).
* ``weekly_refresh`` -- ``weekly_refresh``: Demands open for more than 14 days with no sourcing run in the last 7
  days (and the trigger enabled) emit ``SourcingRefreshDue`` (dedup ``sourcing:refresh:<demand>:<ISO week>``).
* ``manual`` -- ``start_manual_run`` (``POST /demands/{id}/sourcing-runs``; owner = the person).
* ``DemandStatusChanged`` away from ``open`` -> ``cancel_run(reason_code="subject_closed")`` for active runs.
* ``ProposalDecided`` of ``outreach.send`` -> the entry's outreach status.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.core.events import emit
from app.events.sourcing_agent import SourcingRefreshDue, SourcingRerankRequested
from app.models.agent_runtime import ACTIVE_RUN_STATUSES, AgentConfig, AgentRun
from app.schemas.sourcing_agent import SourcingRunOut

AGENT_KEY = "sourcing"
REFRESH_OPEN_DAYS = 14
REFRESH_GAP_DAYS = 7


def kickoff_inputs(
    demand_id: uuid.UUID, trigger_key: str, settings: Any, previous_id: uuid.UUID | None
) -> Kickoff:
    return Kickoff(
        inputs={
            "demand_id": str(demand_id),
            "trigger_key": trigger_key,
            "top_n": str(settings.top_n),
            "draft_top": str(settings.draft_top),
            "preferred_channel": settings.preferred_channel,
            "max_searches": str(settings.max_searches),
            "propose_pipeline_adds": "yes" if settings.propose_pipeline_adds else "no",
            "propose_sequence_id": str(settings.propose_sequence_id)
            if settings.propose_sequence_id
            else "none",
            "previous_shortlist_id": str(previous_id) if previous_id else "none",
        }
    )


async def start_manual_run(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, *, owner_user_id: uuid.UUID
) -> SourcingRunOut:
    from app.services import agent_runtime_service as runtime
    from app.services.demands import matching_port
    from app.services.sourcing_agent import service, shortlists

    await matching_port.get_demand(ctx, db, demand_id)
    settings = await service.settings_row(db, service._tenant_id(ctx, db))
    previous = await shortlists.current(db, demand_id)
    ref = await runtime.start_run(
        ctx, db, agent_key=AGENT_KEY, trigger_kind="manual", trigger_key="manual", subject=("demand", demand_id),
        kickoff=kickoff_inputs(demand_id, "manual", settings, previous.id if previous else None),
        owner_user_id=owner_user_id, dedup_key=f"sourcing:manual:{demand_id}",
    )  # fmt: skip
    return SourcingRunOut(run_id=ref.run_id, status=ref.status, reason=ref.reason)


async def on_match_run(db: AsyncSession, demand_id: uuid.UUID, match_run_id: uuid.UUID, status: str) -> bool:
    from app.models.matching import MatchRun
    from app.services.demands import matching_port

    if status != "completed":
        return False
    run = await db.get(MatchRun, match_run_id)
    if run is None or run.trigger != "requirements_changed":
        return False
    if await matching_port.demand_status(db, demand_id) != "open":
        return False
    emit(db, SourcingRerankRequested(tenant_id=run.tenant_id, demand_id=demand_id, match_run_id=match_run_id))
    return True


async def cancel_for_demand(db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID) -> int:
    from app.services import agent_runtime_service as runtime

    ids = list(
        (
            await db.execute(
                select(AgentRun.id).where(
                    AgentRun.agent_key == AGENT_KEY,
                    AgentRun.subject_type == "demand",
                    AgentRun.subject_id == demand_id,
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
        ).scalars()
    )
    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    for run_id in ids:
        await runtime.cancel_run(
            ctx, db, run_id, reason="The Demand is no longer open", reason_code="subject_closed"
        )
    return len(ids)


def iso_week(at: datetime) -> str:
    year, week, _ = at.isocalendar()
    return f"{year}-W{week:02d}"


async def weekly_refresh(db: AsyncSession, tenant_id: uuid.UUID, at: datetime | None = None) -> int:
    """Emit ``SourcingRefreshDue`` for each qualifying open Demand (the runtime starts the deduplicated runs)."""
    from sqlalchemy import func

    from app.models.demands import Demand
    from app.services.agent_runtime.state import gates

    now = at or datetime.now(UTC)
    g = await gates(db)
    if g.any_kill or not g.agentic_on or not g.agent_on(AGENT_KEY):
        return 0
    config = (
        await db.execute(select(AgentConfig).where(AgentConfig.agent_key == AGENT_KEY))
    ).scalar_one_or_none()
    if config is not None and (config.triggers_enabled or {}).get("weekly_refresh") is False:
        return 0
    recent = (
        select(AgentRun.subject_id)
        .where(
            AgentRun.agent_key == AGENT_KEY,
            AgentRun.subject_type == "demand",
            AgentRun.created_at > now - timedelta(days=REFRESH_GAP_DAYS),
        )
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Demand.id).where(
            Demand.status == "open",
            Demand.archived_at.is_(None),
            func.coalesce(Demand.opened_at, Demand.created_at) < now - timedelta(days=REFRESH_OPEN_DAYS),
            Demand.id.not_in(recent),
        )
    )
    week = iso_week(now)
    count = 0
    for demand_id in rows.scalars():
        emit(db, SourcingRefreshDue(tenant_id=tenant_id, demand_id=demand_id, week=week))
        count += 1
    return count


async def on_proposal_decided(db: AsyncSession, proposal_id: uuid.UUID, status: str) -> int:
    from app.services.sourcing_agent import shortlists

    return await shortlists.on_proposal_decided(db, proposal_id, status)


async def run_trigger_key(db: AsyncSession, run_id: uuid.UUID | None) -> str:
    """Return the shortlist ``trigger_key`` of a run (``manual`` when unknown, ``workflow`` for workflow runs)."""
    from app.models.sourcing_agent import TRIGGER_KEYS

    run = await db.get(AgentRun, run_id) if run_id is not None else None
    if run is None:
        return "manual"
    if run.trigger_key in TRIGGER_KEYS:
        return str(run.trigger_key)
    return "workflow" if run.trigger_kind == "workflow" else "manual"


def handler_payload(e: Any, fields: Mapping[str, str]) -> dict[str, str]:
    return {k: str(getattr(e, attr)) for k, attr in fields.items()}


__all__ = [
    "AGENT_KEY",
    "cancel_for_demand",
    "iso_week",
    "kickoff_inputs",
    "on_match_run",
    "on_proposal_decided",
    "run_trigger_key",
    "start_manual_run",
    "weekly_refresh",
]
