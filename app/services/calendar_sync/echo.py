# ruff: noqa: E501
"""Echo detection (Epic 72 spec A5): a change is Talent OS's own write coming back when its etag equals the one we
stored after the write, or it carries our current version tag at the Interview's times and is not deleted."""

from datetime import datetime

from app.models.calendar_sync import CalendarEventLink
from app.services.calendar_sync.common import MOVE_TOLERANCE
from app.services.connections.provider import EventChange


def same_time(a: datetime | None, b: datetime | None) -> bool:
    return a is not None and b is not None and abs(a - b) < MOVE_TOLERANCE


def is_deleted(change: EventChange) -> bool:
    return change.status in ("deleted", "cancelled")


def is_echo(link: CalendarEventLink, change: EventChange, starts_at: datetime, ends_at: datetime) -> bool:
    if is_deleted(change):
        return False
    if change.etag and link.tos_etag and change.etag == link.tos_etag:
        return True
    return (
        change.tos_tag is not None
        and change.tos_tag == link.tos_tag
        and same_time(change.start_utc, starts_at)
        and same_time(change.end_utc, ends_at)
    )
