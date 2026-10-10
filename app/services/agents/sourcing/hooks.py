"""Epic 56: replace the ``sourcing`` placeholder with the Sourcing & Matching Agent."""

from app.core.agentic import register_agent
from app.services.agents.sourcing.definition import SPEC

register_agent(SPEC)
