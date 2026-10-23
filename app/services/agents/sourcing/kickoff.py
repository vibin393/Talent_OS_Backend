# ruff: file-ignore[line-too-long]
"""Kickoff of the ``sourcing`` agent (Epic 56). Trusted ids / labels only -- no untrusted text.

Runs started by the sourcing domain (manual, requirements change, weekly refresh) pass a full Kickoff with the tenant
settings; event-triggered runs built by the runtime get the Demand id and the trigger key here, and the agent reads
the settings with ``shortlists.get_for_demand`` first.
"""

from collections.abc import Mapping
from typing import Any

from app.core.agentic import Kickoff


def kickoff(payload: Mapping[str, Any]) -> Kickoff:
    demand_id = payload.get("subject_id") if payload.get("subject_type") == "demand" else None
    inputs = {
        "demand_id": str(demand_id) if demand_id else "none",
        "trigger_key": str(payload.get("trigger_key") or "manual"),
        "settings": "read them with shortlists.get_for_demand",
    }
    return Kickoff(inputs=inputs)


__all__ = ["kickoff"]
