"""Kickoff of the BGV Agent (Epic 62): ids only."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    return Kickoff(
        inputs={
            "case_id": str(payload.get("subject_id") or "none"),
            "trigger": str(payload.get("trigger_key") or "manual"),
            "today": datetime.now(UTC).date().isoformat(),
        }
    )


__all__ = ["kickoff"]
