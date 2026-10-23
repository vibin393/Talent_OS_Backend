# ruff: file-ignore[line-too-long]
"""Credential expiry status (spec §5.7, Epic 21 A5/A6). Pure functions: no I/O.

* ``status_for(expires_on, today, offsets)``: ``expired`` when ``today > expires_on``; ``expiring_soon`` when
  ``0 <= days_left <= max(offsets)``; ``none`` otherwise (or without an expiry date).
* ``alert_plan(expires_on, today, offsets, sent)``: the alert (offset days, or ``EXPIRED_OFFSET``) due today and
  every offset it covers (marked sent together, so a late-recorded credential gets one alert, not three).
"""

from collections.abc import Iterable, Sequence
from datetime import date

EXPIRED_OFFSET = -1


def days_left(expires_on: date | None, today: date) -> int | None:
    return None if expires_on is None else (expires_on - today).days


def status_for(expires_on: date | None, today: date, offsets: Sequence[int]) -> str:
    left = days_left(expires_on, today)
    if left is None:
        return "none"
    if left < 0:
        return "expired"
    if left <= max(offsets or (0,)):
        return "expiring_soon"
    return "none"


def alert_plan(
    expires_on: date | None, today: date, offsets: Sequence[int], sent: Iterable[int]
) -> tuple[int | None, list[int]]:
    """``(offset_to_send | None, offsets_to_mark)``."""
    left = days_left(expires_on, today)
    if left is None:
        return None, []
    already = set(sent)
    if left < 0:
        due = [o for o in offsets if o not in already] + [EXPIRED_OFFSET]
        return (EXPIRED_OFFSET if EXPIRED_OFFSET not in already else None), [
            o for o in due if o not in already
        ]
    due = sorted(o for o in offsets if left <= o)
    if not due:
        return None, []
    target = due[0]
    mark = [o for o in due if o not in already]
    return (target if target not in already else None), mark
