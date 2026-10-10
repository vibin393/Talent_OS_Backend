# ruff: noqa: E501
"""The ``copilot_candidate`` ``AgentSpec`` (Epic 77 A2, FR-396..FR-400, AD-89): fills the Epic 66 placeholder.

* Origin ``portal``: the run is owned by the signed-in portal principal (``OwnerRef``); Effective Rights = this
  ceiling ∩ the principal's portal permissions, re-read every step (a revoked user fails the next step closed).
* Tools: only the ``portal_candidate.*`` tools (their ``portal_candidate`` exposures; ``tests/portal_copilot`` asserts the two
  lists match). Reads execute; every write is a Draft the principal confirms (policy branch ``portal``).
* The ceiling holds portal permission keys only -- never an internal permission key.
"""

from app.core.agentic import AgentSpec, RunLimits
from app.core.permissions import Scope

READ_TOOLS: tuple[str, ...] = (
    "portal_candidate.list_applications",
    "portal_candidate.get_application",
    "portal_candidate.upcoming_interviews",
    "portal_candidate.requested_documents",
    "portal_candidate.offer_status",
    "portal_candidate.search_knowledge",
    "portal_candidate.search_jobs",
    "portal_candidate.screen_for",
)
DRAFT_TOOLS: tuple[str, ...] = (
    "portal_candidate.draft_profile_update",
    "portal_candidate.draft_reschedule_request",
    "portal_candidate.draft_message",
    "portal_candidate.draft_handoff",
)
CEILING_KEYS: tuple[str, ...] = ("portal_copilot:use", "self:read", "self:draft")

SPEC = AgentSpec(
    key="copilot_candidate",
    version=1,
    owner_epic="77",
    name="Candidate Copilot",
    purpose="Answers a Candidate's questions about their applications, interviews, documents and offers, and prepares drafts they confirm.",
    prompt_id="agent_copilot_candidate",
    model_tier="standard",
    effort="low",
    tools=(*READ_TOOLS, *DRAFT_TOOLS),
    permission_ceiling=dict.fromkeys(CEILING_KEYS, Scope.TENANT),
    default_mode="suggest",
    limits=RunLimits(
        max_steps=8,
        max_tool_calls=12,
        max_writes=3,
        max_tokens_per_run=60_000,
        step_timeout_s=60,
        wall_clock_s=120,
        max_parallel_tool_calls=4,
        max_wait_hours=0,
    ),
    origin="portal",
    fallback="none",
    consent_purposes=(),
    eval_set="portal_copilot_candidate",
    eval_threshold=0.9,
)

__all__ = ["CEILING_KEYS", "DRAFT_TOOLS", "READ_TOOLS", "SPEC"]
