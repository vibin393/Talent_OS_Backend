# ruff: file-ignore[line-too-long]
"""Kickoff of the Billing Agent (Epic 63): ids only -- ``{legal_entity_id, period_start, period_end, client_id}``. The
"Prepare invoice run" endpoint and the billing-day tick pass them explicitly; a bare manual start (foundation
``POST /agent-runs``, which takes no kickoff inputs -- spec Known gaps 5) prepares the previous calendar month."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    first = datetime.now(UTC).date().replace(day=1)
    end = first - timedelta(days=1)
    return Kickoff(
        inputs={
            "legal_entity_id": str(payload.get("subject_id") or "none"), "period_start": end.replace(day=1).isoformat(),
            "period_end": end.isoformat(), "client_id": "none",
        }
    )  # fmt: skip


__all__ = ["kickoff"]
