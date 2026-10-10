"""Epic 62: replace the compliance placeholder with the real agent."""

from app.core.agentic import register_agent
from app.services.agents.compliance.spec import SPEC

register_agent(SPEC)
