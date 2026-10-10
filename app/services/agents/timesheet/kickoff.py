# ruff: noqa: E501
"""Kickoff of the Timesheet Agent (Epic 63): ids only. Nudge runs: ``{run_kind: nudge, account_manager_id, today,
timezone}``; flag runs: ``{run_kind: flags, timesheet_id, version}``. The domain ticks pass these explicitly; this
builder covers manual / workflow starts from the subject."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    subject_type, subject_id = payload.get("subject_type"), payload.get("subject_id")
    if subject_type == "timesheet":
        return Kickoff(inputs={"run_kind": "flags", "timesheet_id": str(subject_id), "version": "current"})
    return Kickoff(
        inputs={
            "run_kind": "nudge", "account_manager_id": str(subject_id or "none"),
            "today": datetime.now(UTC).date().isoformat(), "timezone": "tenant local",
        }
    )  # fmt: skip


__all__ = ["kickoff"]
