"""Epic 64: replace the ``analytics`` placeholder and register the Copilot capability ``analytics``."""

from app.core.agentic import register_agent
from app.services.agents.analytics import capability
from app.services.agents.analytics.definition import SPEC

register_agent(SPEC)
capability.register()
