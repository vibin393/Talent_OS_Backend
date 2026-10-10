"""Epic 62: replace the bgv placeholder with the real agent."""

from app.core.agentic import register_agent
from app.services.agents.bgv.spec import SPEC

register_agent(SPEC)
