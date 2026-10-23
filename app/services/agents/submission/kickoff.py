# ruff: file-ignore[line-too-long]
"""Kickoff of the ``submission`` agent (Epic 61). Trusted ids / labels only -- no untrusted text: screening, interview
and match text arrives through ``submissions.get_pack_context`` (untrusted output, the run is tainted then). The word
limit and the administrator's write-up guidance are per Client: the agent reads them from the same tool (``rules``).

``inputs = {"application_id", "trigger", "now", "timezone", "max_words", "write_up_instructions"}``.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.agentic import Kickoff

TIMEZONE = "Asia/Kolkata"


def build(payload: Mapping[str, Any]) -> Kickoff:
    application_id = payload.get("subject_id") if payload.get("subject_type") == "application" else None
    inputs = {
        "application_id": str(application_id) if application_id else "none",
        "trigger": str(payload.get("trigger_key") or "manual"),
        "now": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "timezone": TIMEZONE,
        "max_words": "rules.write_up_max_words from submissions__get_pack_context",
        "write_up_instructions": "rules.write_up_instructions from submissions__get_pack_context (trusted, may be empty)",
    }
    return Kickoff(inputs=inputs)


__all__ = ["build"]
