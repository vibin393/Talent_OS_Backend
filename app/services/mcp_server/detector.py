# ruff: noqa: E501
"""Bulk-extraction detector (Epic 73, spec 0.6; FR-380). Pure decision over the limits counters.

After every executed call the candidate ids it touched (``ExternalCallOut.candidate_ids``, D-25(b)) are added to the
session-hour set. When the set grows past the tenant threshold (default 500) the session is paused, the user's MCP
access is held and AI Administrators are told (the facade performs those effects)."""

import dataclasses
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.services.mcp_server import limits


@dataclasses.dataclass(frozen=True, slots=True)
class Observation:
    distinct: int
    tripped: bool


async def observe(
    session_key: str, candidate_ids: Iterable[Any], threshold: int, *, now: datetime | None = None
) -> Observation:
    ids = list(candidate_ids)
    if not ids:
        return Observation(distinct=0, tripped=False)
    size = await limits.add_candidates(session_key, ids, now=now)
    return Observation(distinct=size, tripped=size > threshold)


__all__ = ["Observation", "observe"]
