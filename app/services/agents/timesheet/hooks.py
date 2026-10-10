"""Epic 63: replace the ``timesheet`` placeholder with the Timesheet Agent."""

from app.core.agentic import register_agent
from app.services.agents.timesheet.spec import SPEC

register_agent(SPEC)
