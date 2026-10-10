# ruff: noqa: E501
"""The ``campaign_copy`` AgentSpec (Epic 78 A3, AD-91, FR-401). Read tools only, no write tools: the run ends with
``emit_result`` validated against ``CampaignCopyResult`` (``extra="forbid"``); ``campaign_copy`` hooks turn it into
drafts. The agent never sees recipient records and cannot apply, submit, schedule or send anything. Owner = the
requester. ``fallback="none"``: a failed run marks the request failed with the template fallback message."""

from app.core.agentic import AgentSpec, RunLimits
from app.core.permissions import Scope
from app.schemas.campaign_copy import CampaignCopyResult

KEY = "campaign_copy"
TOOLS: tuple[str, ...] = (
    "campaign_copy.get_brief", "campaign_copy.get_postings", "campaign_copy.get_templates", "campaign_copy.get_tenant_style",
)  # fmt: skip
PERMISSION_CEILING = {"campaign:read": Scope.TENANT}

SPEC = AgentSpec(
    key=KEY, version=1, owner_epic="78", name="Campaign Copy Agent",
    purpose="Drafts recruitment-marketing copy (email subject / preheader / body or SMS / WhatsApp template values) per audience segment from the brief, aggregate segment definitions and linked postings' public fields. Drafts need a person's approval before use.",
    prompt_id="agent_campaign_copy", model_tier="standard", effort="medium", tools=TOOLS, permission_ceiling=PERMISSION_CEILING,
    default_mode="auto",
    limits=RunLimits(max_steps=6, max_tool_calls=10, max_writes=1, max_tokens_per_run=60_000, max_wait_hours=0),
    origin="agent", result_model=CampaignCopyResult, fallback="none", consent_purposes=(),
    eval_set="evals/campaign_copy", eval_threshold=0.9,
)  # fmt: skip

__all__ = ["KEY", "PERMISSION_CEILING", "SPEC", "TOOLS"]
