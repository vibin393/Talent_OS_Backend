# ruff: file-ignore[line-too-long]
"""The ``demand`` AgentSpec (Epic 55, spec A4; FR-281..FR-284).

* Run kinds (``kickoff.inputs.mode``): ``intake`` (subject = an intake message; owner = the matched Client's Account
  Manager, else the fallback owner, or the forwarding colleague), ``draft_review`` / ``opened_review`` / ``manual`` /
  ``sla_check`` (subject = a demand; owner = the demand owner) and ``sla_scan`` (no subject; owner = the configured scan
  owner). Effective Rights = owner ∩ ``permission_ceiling``.
* ★ never: submitting for approval, approving, any demand status change, publishing / unpublishing a posting. No tool
  in the allow-list declares a transition.
* Extraction is a separate fast-tier call inside ``demands.extract_intake`` (schema-bound, quote-checked).
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.agentic import AgentSpec, RunLimits
from app.core.permissions import Scope
from app.services.agents.demand.kickoff import kickoff
from app.services.agents.demand.triggers import TRIGGERS, owner

KEY = "demand"
T = Scope.TENANT

READ_TOOLS: tuple[str, ...] = (
    "demands.get_intake",
    "demands.extract_intake",
    "clients.match_sender",
    "contracts.for_client",
    "rate_cards.lookup",
    "demands.get_for_review",
    "skills.normalise",
    "demands.search_similar",
    "demands.preview_jd",
    "sla.status",
    "sla.risk_scan",
)
WRITE_TOOLS: tuple[str, ...] = (
    "demands.create_draft",
    "demands.update_draft",
    "demands.update",
    "demands.suggest_skills",
    "demands.flag_duplicate",
    "sla.send_digest",
)
CORE_TOOLS: tuple[str, ...] = (
    "tasks.create",
    "notes.add",
    "flags.raise",
    "notify.user",
    "messaging.create_draft",
)
TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS + CORE_TOOLS

PERMISSION_CEILING = dict.fromkeys(
    (
        "demand:read", "demand:create", "demand:update", "client:read", "contact:read", "contract:read",
        "rate_card:read", "task:create", "note:write", "prediction:read", "message:draft", "compensation:view",
    ),
    T,
)  # fmt: skip


class DemandAgentResult(BaseModel):
    """``emit_result`` schema: ids and counts only."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "needs_human", "nothing_to_do"]
    summary: str = Field(max_length=2000)
    drafted_demand_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    flags_raised: int = Field(default=0, ge=0, le=100)


def fallback_link(subject: tuple[str, uuid.UUID] | None) -> str:
    if subject is None:
        return "/demands"
    if subject[0] == "demand_intake_message":
        return f"/demands/intake?message={subject[1]}"
    return f"/demands/{subject[1]}"


SPEC = AgentSpec(
    key=KEY,
    version=1,
    owner_epic="55",
    name="Demand Agent",
    purpose="Turns client requests into draft demands, drafts JDs and skills, flags possible duplicates and warns about SLA risk.",
    prompt_id="agent_demand",
    model_tier="standard",
    effort="medium",
    tools=TOOLS,
    permission_ceiling=PERMISSION_CEILING,
    default_mode="suggest",
    limits=RunLimits(
        max_steps=12, max_tool_calls=30, max_writes=10, max_tokens_per_run=150_000, step_timeout_s=90,
        wall_clock_s=600, max_parallel_tool_calls=6, max_wait_hours=0,
    ),
    subject_type=None,
    origin="agent",
    triggers=TRIGGERS,
    owner=owner,
    kickoff=kickoff,
    result_model=DemandAgentResult,
    fallback="owner_task",
    fallback_link=fallback_link,
    channels=("email",),
    consent_purposes=("ai_processing",),
    aedt_feature=None,
    eval_set="evals/demand",
    eval_threshold=0.9,
)  # fmt: skip

__all__ = [
    "CORE_TOOLS",
    "KEY",
    "PERMISSION_CEILING",
    "READ_TOOLS",
    "SPEC",
    "TOOLS",
    "WRITE_TOOLS",
    "DemandAgentResult",
]
