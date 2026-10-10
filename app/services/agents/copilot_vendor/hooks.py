"""Epic 77: replace the ``copilot_vendor`` placeholder with the real portal Copilot agent."""

from app.core.agentic import register_agent
from app.services.agents.copilot_vendor.definition import SPEC

register_agent(SPEC)
