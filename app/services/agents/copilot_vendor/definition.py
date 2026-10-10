# ruff: noqa: E501
"""The ``copilot_vendor`` ``AgentSpec`` (Epic 77 A2, FR-396..FR-400, AD-89): fills the Epic 66 placeholder.

* Origin ``portal``: the run is owned by the signed-in portal principal (``OwnerRef``); Effective Rights = this
  ceiling ∩ the principal's portal permissions, re-read every step (a revoked user fails the next step closed).
* Tools: only the ``portal_vendor.*`` tools (their ``portal_vendor`` exposures; ``tests/portal_copilot`` asserts the two
  lists match). Reads execute; every write is a Draft the principal confirms (policy branch ``portal``).
* The ceiling holds portal permission keys only -- never an internal permission key.
"""

from app.core.agentic import AgentSpec, RunLimits
from app.core.permissions import Scope

READ_TOOLS: tuple[str, ...] = (
    "portal_vendor.list_releases",
    "portal_vendor.get_release",
    "portal_vendor.list_submissions",
    "portal_vendor.get_submission",
    "portal_vendor.list_placements",
    "portal_vendor.list_statements",
    "portal_vendor.screen_for",
)
DRAFT_TOOLS: tuple[str, ...] = ("portal_vendor.draft_candidate_profile", "portal_vendor.draft_message")
CEILING_KEYS: tuple[str, ...] = (
    "portal_copilot:use",
    "releases:view",
    "submissions:create",
    "placements:view",
    "statements:view",
)

SPEC = AgentSpec(
    key="copilot_vendor",
    version=1,
    owner_epic="77",
    name="Vendor Portal Copilot",
    purpose="Answers a Vendor User's questions about releases, submissions, placements and statements, and prepares drafts they confirm.",
    prompt_id="agent_copilot_vendor",
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
    eval_set="portal_copilot_vendor",
    eval_threshold=0.9,
)

__all__ = ["CEILING_KEYS", "DRAFT_TOOLS", "READ_TOOLS", "SPEC"]
