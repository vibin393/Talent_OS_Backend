"""Epic 61: replace the ``submission`` placeholder with the Submission Agent."""

from app.core.agentic import register_agent
from app.services.agents.submission.spec import SPEC

register_agent(SPEC)
