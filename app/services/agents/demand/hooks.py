"""Epic 55: replace the ``demand`` placeholder with the Demand Agent and register the Copilot capability
``draft_demand`` (C-4: on when ``agent_demand`` is enabled and the user holds ``demand:create``)."""

from app.core.agentic import (
    CopilotCapability,
    copilot_capabilities,
    register_agent,
    register_copilot_capability,
)
from app.services.agents.demand.definition import SPEC

register_agent(SPEC)

if "draft_demand" not in copilot_capabilities():
    register_copilot_capability(
        CopilotCapability(
            key="draft_demand",
            owner_epic="55",
            title="Draft a demand",
            description="Prepare a draft demand from the conversation (you confirm it)",
            tools=("demands.create_draft", "contracts.for_client", "rate_cards.lookup"),
            agent_key="demand",
            permission="demand:create",
        )
    )
