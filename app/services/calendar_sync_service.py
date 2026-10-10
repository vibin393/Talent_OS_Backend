# ruff: noqa: E501
"""Facade of the ``calendar_sync`` domain (Epic 72). Routers, tasks and other domains call these, never the internals.

* settings: ``get_settings`` / ``update_settings``; my sync: ``mine``, ``resync_mine``, ``set_optout``;
* interviews: ``interview_sync`` (+ ``sync_health``), ``recreate_event``, ``keep_talent_os_time``, ``dismiss_change``;
* admin: ``list_channels``; ingress: ``handle_push``;
* jobs: ``ensure_channel``, ``ensure_for_interview``, ``backfill``, ``run_delta``, ``renew_due``, ``poll_due``.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.calendar_sync import channels, delta, ingress
from app.services.calendar_sync import settings as _settings
from app.services.calendar_sync.ingress import IngressResult

get_settings = _settings.get
update_settings = _settings.update
mine = _settings.mine
resync_mine = _settings.resync_mine
set_optout = _settings.set_optout
interview_sync = _settings.interview_sync
sync_health = _settings.sync_health
recreate_event = _settings.recreate_event
keep_talent_os_time = _settings.keep_time
dismiss_change = _settings.dismiss
list_channels = _settings.list_channels
handle_push = ingress.handle
ensure_channel = channels.ensure_channel
renew_due = channels.renew_due
poll_due = channels.poll_due


async def ensure_for_interview(db: AsyncSession, interview_id: uuid.UUID) -> int:
    """Ensure channels for the organiser and every internal participant with an active connection."""
    from app.models.connections import UserConnection
    from app.models.interviews import Interview, InterviewParticipant

    row = await db.get(Interview, interview_id)
    if row is None or row.status != "scheduled":
        return 0
    users = {row.organizer_user_id} | set(
        (
            await db.execute(
                select(InterviewParticipant.user_id).where(
                    InterviewParticipant.interview_id == row.id,
                    InterviewParticipant.user_id.is_not(None),
                    InterviewParticipant.removed_at.is_(None),
                )
            )
        ).scalars()
    )
    conns = list(
        (
            await db.execute(
                select(UserConnection.id).where(UserConnection.user_id.in_(users), UserConnection.status == "active")
            )
        ).scalars()
    )
    count = 0
    for cid in conns:
        if await channels.ensure_channel(db, cid) is not None:
            count += 1
    return count


async def backfill(db: AsyncSession) -> int:
    count = 0
    for cid in await channels.connections_to_backfill(db):
        if await channels.ensure_channel(db, cid) is not None:
            count += 1
    return count


async def run_delta(db: AsyncSession, channel_id: uuid.UUID, *, full: bool = False) -> dict[str, Any]:
    ch = await channels.by_channel_id(db, channel_id)
    if ch is None or ch.status not in ("active", "renewing", "polling"):
        return {"status": "skipped"}
    return await delta.run(db, ch, full=full)


__all__ = [
    "IngressResult",
    "backfill",
    "dismiss_change",
    "ensure_channel",
    "ensure_for_interview",
    "get_settings",
    "handle_push",
    "interview_sync",
    "keep_talent_os_time",
    "list_channels",
    "mine",
    "poll_due",
    "recreate_event",
    "renew_due",
    "resync_mine",
    "run_delta",
    "set_optout",
    "sync_health",
    "update_settings",
]
