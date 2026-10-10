"""Epic 57: replace the ``screening`` placeholder with the Screening Agent."""

from app.core.agentic import register_agent
from app.services.agents.screening.definition import SPEC

register_agent(SPEC)
