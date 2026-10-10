# ruff: noqa: E501
"""Kickoff of the ``demand`` agent (Epic 55, spec 0.1). Deterministic, ids only -- no untrusted text: the model reads
the email body and documents through tools, whose output is untrusted (the run is tainted then).

``inputs = {"mode", "subject_id", "today", "fy_start_month": "4"}``; the mode follows the trigger key.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.agentic import Kickoff

MODES: dict[str, str] = {
    "intake_received": "intake",
    "draft_created": "draft_review",
    "demand_opened": "opened_review",
    "sla_changed": "sla_check",
    "daily_sla_scan": "sla_scan",
}


def mode_for(trigger_key: str | None, subject_type: str | None) -> str:
    if trigger_key in MODES:
        return MODES[trigger_key]
    if subject_type == "demand_intake_message":
        return "intake"
    if subject_type is None:
        return "sla_scan"
    return "manual"


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    mode = mode_for(payload.get("trigger_key"), payload.get("subject_type"))
    inputs = {
        "mode": mode,
        "subject_id": str(payload.get("subject_id") or "none"),
        "today": datetime.now(UTC).date().isoformat(),
        "fy_start_month": "4",
    }
    if mode == "manual":
        inputs["note"] = (
            "Read the demand first; use the draft_review steps for drafts and opened_review otherwise."
        )
    return Kickoff(inputs=inputs)


__all__ = ["MODES", "kickoff", "mode_for"]
