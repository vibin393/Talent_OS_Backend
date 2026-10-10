# ruff: noqa: E501
"""Kickoff of the Compliance Agent (Epic 62): ids only -- the plan is read through ``compliance.get_chase_plan``."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    return Kickoff(
        inputs={
            "candidate_id": str(payload.get("subject_id") or "none"),
            "today": datetime.now(UTC).date().isoformat(),
            "timezone": "tenant local",
            "trigger": str(payload.get("trigger_key") or "manual"),
        }
    )


__all__ = ["kickoff"]
