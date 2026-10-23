# ruff: file-ignore[line-too-long]
"""Deterministic shortlist order (Epic 56, AD-71 / AD-8). Pure: no I/O, no model input.

Order: ``overall`` descending (the stored P1 ``matches.overall``; unscored candidates rank after every scored one),
then ``last_activity_at`` descending (unknown last), then ``candidate_id`` ascending. The model only proposes ids
and source tags; it can never change this order.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol


class Rankable(Protocol):
    candidate_id: uuid.UUID
    overall: Decimal | float | None
    last_activity_at: datetime | None


def sort_key(item: Any) -> tuple[int, float, int, float, str]:
    overall = getattr(item, "overall", None) if not isinstance(item, dict) else item.get("overall")
    activity = (
        getattr(item, "last_activity_at", None)
        if not isinstance(item, dict)
        else item.get("last_activity_at")
    )
    cid = getattr(item, "candidate_id", None) if not isinstance(item, dict) else item.get("candidate_id")
    return (
        0 if overall is not None else 1,
        -float(overall) if overall is not None else 0.0,
        0 if activity is not None else 1,
        -activity.timestamp() if activity is not None else 0.0,
        str(cid),
    )


def rank(items: Iterable[Any]) -> list[Any]:
    """Return ``items`` in shortlist order (stable for equal keys, which cannot happen: ids are unique)."""
    return sorted(items, key=sort_key)


def select_top(ranked: Sequence[Any], top_n: int, is_dismissed: Any) -> list[Any]:
    """Return the first ``top_n`` non-dismissed rows of ``ranked`` plus the dismissed rows ranked above the cut (shown
    behind "Show dismissed"); ranks stay contiguous."""
    out: list[Any] = []
    kept = 0
    for row in ranked:
        if kept >= top_n:
            break
        out.append(row)
        if not is_dismissed(row):
            kept += 1
    return out


__all__ = ["Rankable", "rank", "select_top", "sort_key"]
