"""Derived Demand Progress (PRD §5.1; pure, response-only, never user-editable).

Highest applicable wins: partially_filled > offer > interviewing > submitted > recruiting > None.
"""

from typing import Literal

Progress = Literal["recruiting", "submitted", "interviewing", "offer", "partially_filled"]


def compute(
    *,
    positions: int,
    positions_filled: int,
    applications: int,
    submissions: int,
    interviews_scheduled: int,
    offers_released: int,
) -> Progress | None:
    if 0 < positions_filled < positions:
        return "partially_filled"
    if offers_released > 0:
        return "offer"
    if interviews_scheduled > 0:
        return "interviewing"
    if submissions > 0:
        return "submitted"
    if applications > 0:
        return "recruiting"
    return None
