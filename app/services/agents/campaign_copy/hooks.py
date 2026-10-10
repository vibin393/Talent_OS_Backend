"""Epic 78: replace the ``campaign_copy`` placeholder with the real agent."""

from app.core.agentic import register_agent
from app.services.agents.campaign_copy.definition import SPEC

register_agent(SPEC)
