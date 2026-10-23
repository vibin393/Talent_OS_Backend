# ruff: file-ignore[line-too-long]
"""The ``analytics`` AgentSpec (Epic 64, FR-316..FR-318). A Copilot mode ("Ask about your data", origin ``copilot``):
started by the Copilot surface with the user as Accountable Owner; every tool is ``read``."""

from app.core.agentic import AgentSpec, RunLimits
from app.core.permissions import Scope

TOOLS = ("semantic.catalogue", "semantic.validate", "semantic.run", "reports.list", "reports.get")

# Effective Rights = user ∩ this ceiling. The source read keys are pinned against ``core.semantic.sources()`` by
# ``tests/agents/analytics/test_definition.py``. Never ``identity:view``.
SOURCE_READ_KEYS = (
    "candidate:read", "demand:read", "application:read", "submission:read", "interview:read", "offer:read",
    "placement:read", "timesheet:read", "billing:read", "receivables:read", "client:read", "assessment:read",
    "campus:read", "referral:read",
)  # fmt: skip
CEILING = dict.fromkeys(("report:read", *SOURCE_READ_KEYS, "compensation:view"), Scope.TENANT)

SPEC = AgentSpec(
    key="analytics",
    version=1,
    owner_epic="64",
    name="Analytics",
    purpose="Answers questions about your data with governed report queries you can open in the report builder.",
    prompt_id="agent_analytics",
    model_tier="standard",
    effort="medium",
    tools=TOOLS,
    permission_ceiling=CEILING,
    default_mode="suggest",
    limits=RunLimits(
        max_steps=8,
        max_tool_calls=14,
        max_writes=1,
        max_tokens_per_run=120_000,
        step_timeout_s=60,
        wall_clock_s=180,
        max_parallel_tool_calls=4,
        max_wait_hours=0,
    ),
    subject_type=None,
    origin="copilot",
    triggers=(),
    owner=None,
    kickoff=None,
    result_model=None,
    fallback="none",
    consent_purposes=("ai_processing",),
    aedt_feature=None,
    eval_set="evals/analytics",
    eval_threshold=0.9,
)

__all__ = ["CEILING", "SOURCE_READ_KEYS", "SPEC", "TOOLS"]
