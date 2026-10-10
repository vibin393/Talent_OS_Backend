# ruff: noqa: E501
"""Default kickoff of the Voice Screening Agent (Epic 58). The domain always starts runs with an explicit kickoff
(``voice_session_id``, ``application_id``, ``mode``, ``provider_key``, ``stage``, questionnaire); a run started without one
(an event delivered to the runtime first, or the workflow action) is told to look the session up by its Application."""

from collections.abc import Mapping
from typing import Any

from app.core.agentic import Kickoff

_STAGES = {"requested": "request", "consented": "consented", "session_ready": "ready"}


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    inputs = {k: str(v) for k, v in payload.items() if v is not None}
    inputs.setdefault("stage", _STAGES.get(str(payload.get("trigger_key") or ""), "ready"))
    inputs.setdefault("voice_session_id", "unknown: finish with emit_result outcome nothing_to_do")
    return Kickoff(inputs=inputs)


__all__ = ["kickoff"]
