# ruff: noqa: E501
"""Default kickoff of the Onboarding Agent (Epic 62). Q&A runs always pass an explicit kickoff from ``knowledge.qa``
(thread id, audience, tenant name + the question as untrusted text); a run started without one (e.g. an eval) is bound
to its subject thread."""

from collections.abc import Mapping
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    return Kickoff(
        inputs={
            "thread_id": str(payload.get("subject_id") or "none"),
            "audience": "worker",
            "tenant_name": "the organisation",
        }
    )


__all__ = ["kickoff"]
