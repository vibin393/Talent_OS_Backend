"""Demands service package (Epic 4, stories 4.1-4.6).

Public API (routers and other services import from here):
``records`` (CRUD, list, ``list_for_client``, hooks ``record_pipeline_counts`` / ``apply_positions_filled``),
``lifecycle`` (``transition_demand``, ``submit_demand``), ``jd`` (versions, AI generation, bias, publishing,
``list_live_postings``), ``intake``, ``assignment`` (``set_assignments``, ``record_first_*`` hooks),
``sla`` (``start_sla_clock``, ``complete_sla_clock``, ``evaluate_tenant``) and ``settings``.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.approvals import RequestOut
from app.services import approvals
from app.services.demands import (
    assignment,
    bias,
    business_hours,
    handlers,
    intake,
    jd,
    lifecycle,
    progress,
    records,
    settings,
    sla,
    transitions,
)
from app.services.demands.assignment import (
    record_first_activity,
    record_first_application,
    record_first_submission,
)
from app.services.demands.common import READ, access_condition, get_for
from app.services.demands.constants import REASON_CODES
from app.services.demands.jd import list_live_postings
from app.services.demands.lifecycle import transition_demand
from app.services.demands.records import (
    apply_positions_filled,
    list_for_client,
    list_query,  # Epic 11: list endpoint + export source
    record_pipeline_counts,
)
from app.services.demands.sla import complete_sla_clock, start_sla_clock

_registered = False


async def demand_access(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, write: bool) -> bool:
    """Access hook for files and activity: may the caller see (or edit) this demand."""
    from app.core.errors import DomainError

    try:
        await get_for(ctx, db, demand_id, "demand:update" if write else READ, for_update=False)
    except DomainError:
        return False
    return True


async def submit(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> dict[str, Any]:
    demand = await lifecycle.submit_demand(ctx, db, demand_id)
    return await records.demand_out(ctx, db, demand)


async def transition(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    to: str,
    reason_code: str | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    demand = await lifecycle.request_transition(ctx, db, demand_id, to, reason_code, comment)
    return await records.demand_out(ctx, db, demand)


async def approval_history(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[RequestOut]:
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    return await approvals.history_for(ctx, db, "demand", demand.id)


def reason_codes() -> dict[str, list[str]]:
    return {k: list(v) for k, v in REASON_CODES.items()}


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    lifecycle.register()
    handlers.register()
    from app.services import files_service

    files_service.register_access_checker("demand", demand_access, plural="demands")
    try:
        from app.services import activity_service
    except ImportError:  # pragma: no cover - Epic 2 absent
        pass
    else:
        activity_service.register_entity("demand", demand_access, plural="demands")
    _registered = True


register()

__all__ = [
    "access_condition",
    "apply_positions_filled",
    "approval_history",
    "assignment",
    "bias",
    "business_hours",
    "complete_sla_clock",
    "demand_access",
    "intake",
    "jd",
    "lifecycle",
    "list_for_client",
    "list_live_postings",
    "list_query",
    "progress",
    "reason_codes",
    "record_first_activity",
    "record_first_application",
    "record_first_submission",
    "record_pipeline_counts",
    "records",
    "settings",
    "sla",
    "start_sla_clock",
    "submit",
    "transition",
    "transition_demand",
    "transitions",
]
