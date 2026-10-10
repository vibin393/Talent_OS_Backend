"""Epic 62: replace the onboarding placeholder with the real agent."""

from app.core.agentic import register_agent
from app.services.agents.onboarding.spec import SPEC

register_agent(SPEC)
