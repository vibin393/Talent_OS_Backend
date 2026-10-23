# ruff: file-ignore[line-too-long]
"""Kickoff of the Collections Agent (Epic 63): ids only -- ``{client_id, today, trigger, promise_id?}``. Client texts
never enter the kickoff; they are read (manual runs only) through ``receivables.get_activity_bodies`` (untrusted)."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    return Kickoff(
        inputs={
            "client_id": str(payload.get("subject_id") or "none"), "today": datetime.now(UTC).date().isoformat(),
            "trigger": str(payload.get("trigger_key") or "manual"),
        }
    )  # fmt: skip


__all__ = ["kickoff"]
