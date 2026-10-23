# ruff: file-ignore[line-too-long]
"""The ``sourcing`` AgentSpec (Epic 56, spec §A4; FR-285..FR-288).

* Subject: the Demand; Accountable Owner: the Demand's owner (primary recruiter). No owner -> no run and the
  foundation's "Agent could not run: no owner" Task (``owner_unavailable``).
* Effective Rights = ``permission_ceiling`` ∩ the recruiter's own rights.
* Triggers: ``demand_opened`` (P1 ``RediscoveryCompleted``, the first match run after a Demand opens),
  ``requirements_changed`` / ``weekly_refresh`` (domain-filtered events emitted by ``sourcing_agent``), ``manual`` and
  ``workflow``.
* Fallback: an owner Task linking the deterministic P1 match list (FR-275).
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits, TriggerSpec
from app.core.permissions import Scope
from app.services.agents.sourcing.kickoff import kickoff

KEY = "sourcing"
T = Scope.TENANT

TOOLS: tuple[str, ...] = (
    "shortlists.get_for_demand",
    "matching.list_ranked_matches",
    "rediscovery.summary",
    "pools.members",
    "search.search_candidates",
    "candidates.engagement",
    "messaging.cap_status",
    "shortlists.save_suggested",
    "outreach.create_draft",
    "outreach.send",
    "pipeline.add_candidate",
    "sequences.enrol",
    "consent.status",
    "notes.add",
    "tasks.create",
    "flags.raise",
)
PERMISSION_CEILING = dict.fromkeys(
    (
        "demand:read", "candidate:read", "match:read", "pool:read", "note:write", "task:create", "message:draft",
        "email:send", "messaging:send", "application:create", "sequence:write",
    ),
    T,
)  # fmt: skip


class SourcingResult(BaseModel):
    """``emit_result`` schema: ids and counts only."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)
    shortlist_id: uuid.UUID | None = None
    drafted: int = Field(default=0, ge=0, le=100)
    suppressed: int = Field(default=0, ge=0, le=100)


async def demand_owner(ctx: Any, db: Any, subject: tuple[str, uuid.UUID] | None) -> uuid.UUID | None:  # ruff: ignore[unused-function-argument]
    """Return the Demand's ``owner_id`` (primary recruiter): a read-only lookup through the shared ``entity_refs`` helper
    (the agent package never imports models or the database, AD-56)."""
    if subject is None or subject[0] != "demand":
        return None
    from app.services import entity_refs

    return await entity_refs.entity_owner_id(db, "demand", subject[1])


def _demand(e: Any) -> tuple[str, uuid.UUID]:
    return ("demand", e.demand_id)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    return f"/demands/{subject[1]}?tab=matches" if subject is not None else "/demands"


TRIGGERS: tuple[TriggerSpec, ...] = (
    TriggerSpec(
        key="demand_opened", kind="event", event="RediscoveryCompleted", event_module="app.events.matching",
        subject=_demand, dedup_key=lambda e: f"sourcing:open:{e.demand_id}", on_active="drop",
    ),
    TriggerSpec(
        key="requirements_changed", kind="event", event="SourcingRerankRequested",
        event_module="app.events.sourcing_agent", subject=_demand,
        dedup_key=lambda e: f"sourcing:req:{e.demand_id}:{e.match_run_id}", on_active="attach",
    ),
    TriggerSpec(
        key="weekly_refresh", kind="event", event="SourcingRefreshDue", event_module="app.events.sourcing_agent",
        subject=_demand, dedup_key=lambda e: f"sourcing:refresh:{e.demand_id}:{e.week}", on_active="drop",
    ),
    TriggerSpec(key="manual", kind="manual"),
    TriggerSpec(key="workflow", kind="workflow"),
)  # fmt: skip

SPEC = AgentSpec(
    key=KEY,
    version=1,
    owner_epic="56",
    name="Sourcing Agent",
    purpose="Finds and ranks people you already know for a Demand and drafts outreach.",
    prompt_id="agent_sourcing",
    model_tier="standard",
    effort="medium",
    tools=TOOLS,
    permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(max_steps=12, max_tool_calls=30, max_writes=10, max_wait_hours=0),
    subject_type="demand",
    origin="agent",
    triggers=TRIGGERS,
    owner=demand_owner,
    kickoff=kickoff,
    result_model=SourcingResult,
    fallback="owner_task",
    fallback_link=fallback_link,
    channels=("email", "whatsapp", "sms"),
    aedt_feature="agent_sourcing",
    eval_set="evals/sourcing",
    eval_threshold=0.9,
)

__all__ = ["KEY", "PERMISSION_CEILING", "SPEC", "TOOLS", "TRIGGERS", "SourcingResult", "demand_owner"]
