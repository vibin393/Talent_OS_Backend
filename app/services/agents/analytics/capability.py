# ruff: file-ignore[line-too-long]
"""Copilot capability ``analytics`` (Epic 64 story 64.4, FR-277): number questions in the general Copilot go through
governed Definitions. On when ``agent_analytics`` is enabled for the tenant and the user holds ``report:read`` (C-4).
``reports.get`` stays analytics-mode only."""

from app.core.agentic import CopilotCapability, copilot_capabilities, register_copilot_capability

CAPABILITY = CopilotCapability(
    key="analytics",
    owner_epic="64",
    title="Analytics",
    description="Answer number questions with governed report queries",
    tools=("semantic.catalogue", "semantic.validate", "semantic.run", "reports.list"),
    prompt_section="copilot_analytics.v1.md",
    agent_key="analytics",
    permission="report:read",
)


def register() -> None:
    if "analytics" not in copilot_capabilities():
        register_copilot_capability(CAPABILITY)


__all__ = ["CAPABILITY", "register"]
