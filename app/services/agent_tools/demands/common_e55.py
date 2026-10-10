# ruff: noqa: E501
"""Shared helpers of the Epic 55 Demand Agent tools (not a tool module: the loader imports only ``tools_*.py``).

Reads and data-scope checks run under ``agent_runtime_service.scope_context`` (the Effective Rights presented as the
Accountable Owner); the actor stays ``agent`` for audit and the ★ guards. In an approved Proposal the context is the
approving person's own.
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.services import agent_runtime_service as runtime

OWNER_EPIC = "55"


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def scoped(tctx: Any) -> Any:
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


async def demand_version(probe: Any) -> str | None:
    """``ScopeRule.version`` of write tools that change a demand: ``demands.updated_at`` (Proposal staleness)."""
    from app.models.demands import Demand

    demand_id = getattr(probe.args, "demand_id", None)
    if demand_id is None:
        return None
    demand = await probe.db.get(
        Demand, demand_id if isinstance(demand_id, uuid.UUID) else uuid.UUID(str(demand_id))
    )
    return demand.updated_at.isoformat() if demand is not None and demand.updated_at else None


async def demand_label(db: Any, demand_id: uuid.UUID) -> str:
    from app.models.demands import Demand

    demand = await db.get(Demand, demand_id)
    return f"{demand.req_id} {demand.title}"[:200] if demand is not None else str(demand_id)


__all__ = ["OWNER_EPIC", "In", "Out", "demand_label", "demand_version", "scoped"]
