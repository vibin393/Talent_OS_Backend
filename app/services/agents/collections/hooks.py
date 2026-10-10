"""Epic 63: replace the ``collections`` placeholder with the Collections Agent."""

from app.core.agentic import register_agent
from app.services.agents.collections.spec import SPEC

register_agent(SPEC)
