# ruff: file-ignore[line-too-long]
"""Default kickoff of the Interview Assistant (Epic 60). Event-triggered runs (``capture_ready``) and runs started
without a domain kickoff get the Interview id only; the agent reads the capture id, interviewers and Kit with
``interviews__get`` / ``interview_kits__get``. Manual re-runs pass ``interview_captures_service.kickoff_for``. Trusted ids
and labels only -- the transcript is read through a tool and is wrapped as untrusted there."""

from collections.abc import Mapping
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    interview_id = payload.get("subject_id") if payload.get("subject_type") == "interview" else None
    return Kickoff(
        inputs={
            "interview_id": str(interview_id) if interview_id else "none",
            "trigger_key": str(payload.get("trigger_key") or "manual"),
            "next": "read interviews.get (capture id, interviewer participant ids), interview_kits.get and interview_capture.transcript",
        }
    )


__all__ = ["kickoff"]
