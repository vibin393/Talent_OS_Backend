# ruff: noqa: E501
"""Default kickoff of the Screening Agent (Epic 57). Conversation runs always pass an explicit kickoff
(``screening_agent.conversations.kickoff``); a run started without one (the workflow action "Start agent run") is
superseded by the conversation run the ``AgentRunStarted`` handler creates, so it is told to finish at once."""

from collections.abc import Mapping
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    inputs = {k: str(v) for k, v in payload.items() if v is not None}
    inputs["mode"] = "superseded"
    return Kickoff(inputs=inputs)


__all__ = ["kickoff"]
