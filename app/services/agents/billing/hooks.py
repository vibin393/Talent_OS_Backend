"""Epic 63: replace the ``billing`` placeholder with the Billing Agent."""

from app.core.agentic import register_agent
from app.services.agents.billing.spec import SPEC

register_agent(SPEC)
