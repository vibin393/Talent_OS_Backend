"""Epic 77: replace the ``copilot_client`` placeholder with the real portal Copilot agent."""

from app.core.agentic import register_agent
from app.services.agents.copilot_client.definition import SPEC

register_agent(SPEC)
