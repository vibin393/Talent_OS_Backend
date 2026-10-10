"""Epic 60: replace the ``interview_assist`` placeholder with the Interview Assistant."""

from app.core.agentic import register_agent
from app.services.agents.interview_assist.definition import SPEC

register_agent(SPEC)
