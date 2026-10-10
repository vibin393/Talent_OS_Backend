"""Epic 58: replace the ``voice_screening`` placeholder with the Voice Screening Agent."""

from app.core.agentic import register_agent
from app.services.agents.voice_screening.definition import SPEC

register_agent(SPEC)
