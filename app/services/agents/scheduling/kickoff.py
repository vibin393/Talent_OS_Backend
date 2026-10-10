# ruff: noqa: E501
"""Default kickoff of the Scheduling Agent (Epic 59). Domain runs (offer / picked / counter / reschedule / reminders /
nudges) always pass an explicit kickoff (``scheduling_agent.requests.kickoff``); a run started without one (the workflow
action "Start agent run") has no request, so it is told to finish at once -- the recruiter starts scheduling with
"Let the agent schedule" (stage, interviewers and duration are needed)."""

from collections.abc import Mapping
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    inputs = {k: str(v) for k, v in payload.items() if v is not None}
    inputs["mode"] = "no_request"
    return Kickoff(inputs=inputs)


__all__ = ["kickoff"]
