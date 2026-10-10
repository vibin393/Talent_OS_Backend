# ruff: noqa: E501
"""The Copilot ``AgentSpec`` (Epic 54 A4, FR-276..FR-280, NFR-45/46/48).

* Owner = the signed-in user (explicit ``owner_user_id`` per turn); Effective Rights = user ∩ ``permission_ceiling``,
  so scope and masking are exactly the user's. ``compensation:view`` is in the ceiling so a holder sees what they
  normally see (masked otherwise). Never ``identity:view``, ``proposal:decide``, ``ai:*``, ``settings:*`` or any
  approve / reject / release / export key.
* Allow-list: the Copilot read tools, the two Copilot draft tools and the core draft tools. The runtime adds the tools
  of enabled Copilot capabilities (C-4). ``messaging.create_draft`` is not listed (a draft of a draft): the Copilot
  offers ``messaging.send`` and the user confirms it. No ★ action is a tool.
* Copilot origin: every non-read tool resolves to ``suggest`` (``copilot_draft``) -> a Draft Action.
"""

from app.core.agentic import AgentSpec, RunLimits
from app.core.permissions import Scope

READ_TOOLS: tuple[str, ...] = (
    "search.global", "candidates.search", "candidates.get", "demands.list", "demands.get", "sla.at_risk",
    "matching.rank", "pipeline.list", "interviews.list", "placements.list", "tasks.list", "activities.list",
    "predictions.get", "clients.get", "navigation.screen_for",
)  # fmt: skip
# ``copilot.propose_slots`` / ``copilot.prepare_pack`` come through Epic 54's own capabilities (``schedule_proposal``,
# ``submission_pack``), so they follow the capability registry like every other capability tool (C-4).
DRAFT_TOOLS: tuple[str, ...] = ("tasks.create", "notes.add", "messaging.send", "messaging.send_templated")
CORE_READ_TOOLS: tuple[str, ...] = ("knowledge.search",)

CEILING_KEYS: tuple[str, ...] = (
    "candidate:read", "application:read", "demand:read", "demand:create", "interview:read", "interview:create",
    "placement:read", "client:read", "contact:read", "task:read", "task:create", "note:write", "prediction:read",
    "report:read", "submission:create", "email:send", "messaging:send", "message:send_templated", "knowledge:read",
    "contract:read", "rate_card:read", "compensation:view",
    # source read keys of the analytics capability (Epic 64)
    "submission:read", "offer:read", "timesheet:read", "billing:read", "receivables:read",
)  # fmt: skip
FORBIDDEN_CEILING_PREFIXES = ("ai:", "settings:")
FORBIDDEN_CEILING_KEYS = frozenset({"identity:view", "proposal:decide"})
FORBIDDEN_CEILING_SUFFIXES = (":approve", ":reject", ":release", ":export")

COPILOT = AgentSpec(
    key="copilot",
    version=1,
    owner_epic="54",
    name="Copilot",
    purpose="Answers questions about your records within your own access and prepares drafts you confirm.",
    prompt_id="agent_copilot",
    model_tier="standard",
    effort="low",
    tools=(*READ_TOOLS, *DRAFT_TOOLS, *CORE_READ_TOOLS),
    permission_ceiling=dict.fromkeys(CEILING_KEYS, Scope.TENANT),
    default_mode="suggest",
    limits=RunLimits(
        max_steps=8,
        max_tool_calls=12,
        max_writes=3,
        max_tokens_per_run=120_000,
        step_timeout_s=60,
        wall_clock_s=120,
        max_parallel_tool_calls=6,
        max_wait_hours=0,
    ),
    subject_type=None,
    origin="copilot",
    triggers=(),
    owner=None,
    kickoff=None,
    result_model=None,
    fallback="none",
    fallback_link=None,
    channels=("email", "sms", "whatsapp"),
    consent_purposes=("ai_processing",),
    aedt_feature=None,
    eval_set="evals/copilot",
    eval_threshold=0.9,
)

__all__ = ["CEILING_KEYS", "COPILOT", "DRAFT_TOOLS", "READ_TOOLS"]
