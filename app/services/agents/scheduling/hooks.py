"""Epic 59: replace the ``scheduling`` placeholder with the Scheduling Agent."""

from app.core.agentic import register_agent
from app.services.agents.scheduling.definition import SPEC

register_agent(SPEC)
