# ruff: noqa: E501
"""Tenant settings, my channels, opt-out, interview sync health and admin views (Epic 72 spec A6 / A8 / A9)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.calendar_sync import CalendarSyncKick
from app.models.calendar_sync import LIVE_CHANNEL_STATUSES, CalendarChannel, CalendarFreeBusyOptout
from app.models.connections import UserConnection
from app.models.interviews import Interview, InterviewParticipant
from app.schemas.calendar_sync import (
    CalendarSyncSettingsIn,
    CalendarSyncSettingsOut,
    ChannelAdminOut,
    EventLinkOut,
    InterviewSyncOut,
    MyChannelOut,
    MySyncOut,
    ParticipantSyncState,
)
from app.services.calendar_sync import links
from app.services.calendar_sync.common import now, settings_row, tenant_of, user_name

ORG_KEYS = ("google_calendar_org", "microsoft_calendar_org")


def _user_id(ctx: RequestContext) -> uuid.UUID:
    p = ctx.principal
    if p is None or getattr(p, "type", None) != "user":
        raise NotFound("Resource not found")
    return p.id  # type: ignore[no-any-return]


async def org_installed(db: AsyncSession) -> bool:
    try:
        from app.models.oauth import AppInstallation

        found = (
            await db.execute(select(AppInstallation.id).where(AppInstallation.connector_key.in_(ORG_KEYS), AppInstallation.status == "active"))
        ).first()
        return found is not None
    except Exception:  # pragma: no cover - marketplace absent
        return False


async def get(ctx: RequestContext, db: AsyncSession) -> CalendarSyncSettingsOut:  # noqa: ARG001
    row = await settings_row(db)
    opted = (await db.execute(select(func.count()).select_from(CalendarFreeBusyOptout))).scalar_one()
    return CalendarSyncSettingsOut(
        enabled=row.enabled, move_policy=row.move_policy, poll_minutes=row.poll_minutes,  # type: ignore[arg-type]
        restore_limit_per_hour=row.restore_limit_per_hour, org_source_enabled=row.org_source_enabled,
        org_source_installed=await org_installed(db), opted_out_count=int(opted),
    )  # fmt: skip


async def update(ctx: RequestContext, db: AsyncSession, data: CalendarSyncSettingsIn) -> CalendarSyncSettingsOut:
    row = await settings_row(db, create=True)
    was_enabled, was_org = row.enabled, row.org_source_enabled
    changes = data.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(row, key, value)
    await db.flush()
    audit.record("calendar_sync.settings_updated", "calendar_sync_settings", row.id, session=db, fields=sorted(changes))
    if row.enabled and not was_enabled:
        emit(db, CalendarSyncKick(tenant_id=tenant_of(db), backfill=True))
    if not row.enabled and was_enabled:
        from app.services.calendar_sync import channels

        for ch in list((await db.execute(select(CalendarChannel).where(CalendarChannel.status.in_(LIVE_CHANNEL_STATUSES)))).scalars()):
            await channels.stop(db, ch)
    if row.org_source_enabled and not was_org:
        from app.services import notifications_service

        await notifications_service.fan_out(db, "org_free_busy_enabled", {"tenant_id": str(tenant_of(db))})
    if was_org and not row.org_source_enabled:
        from app.services.calendar_sync import org_free_busy

        await org_free_busy.clear_cache(tenant_of(db))
    return await get(ctx, db)


async def mine(ctx: RequestContext, db: AsyncSession) -> MySyncOut:
    user_id = _user_id(ctx)
    row = await settings_row(db)
    conns = list((await db.execute(select(UserConnection).where(UserConnection.user_id == user_id, UserConnection.status != "revoked"))).scalars())
    out: list[MyChannelOut] = []
    for c in conns:
        ch = (
            await db.execute(
                select(CalendarChannel).where(CalendarChannel.connection_id == c.id).order_by(CalendarChannel.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        out.append(
            MyChannelOut(
                connection_id=c.id, provider=c.provider, status=ch.status if ch is not None else "none",  # type: ignore[arg-type]
                last_notification_at=ch.last_notification_at if ch else None, last_sync_at=ch.last_sync_at if ch else None,
                poll_minutes=row.poll_minutes,
            )
        )
    opted = (await db.execute(select(CalendarFreeBusyOptout.id).where(CalendarFreeBusyOptout.user_id == user_id))).first()
    return MySyncOut(tenant_enabled=row.enabled, channels=out, org_source_enabled=row.org_source_enabled, free_busy_opted_out=opted is not None)


async def resync_mine(ctx: RequestContext, db: AsyncSession) -> int:
    """Full delta for my live channels (rate limited by the router)."""
    from app.services.calendar_sync import channels, delta

    user_id = _user_id(ctx)
    count = 0
    for c in list((await db.execute(select(UserConnection.id).where(UserConnection.user_id == user_id, UserConnection.status == "active"))).scalars()):
        ch = await channels.live_for(db, c) or await channels.ensure_channel(db, c)
        if ch is not None:
            await delta.run(db, ch, full=True)
            count += 1
    return count


async def set_optout(ctx: RequestContext, db: AsyncSession, opted_out: bool) -> MySyncOut:
    user_id = _user_id(ctx)
    row = (await db.execute(select(CalendarFreeBusyOptout).where(CalendarFreeBusyOptout.user_id == user_id))).scalar_one_or_none()
    if opted_out and row is None:
        db.add(CalendarFreeBusyOptout(id=uuid.uuid4(), tenant_id=tenant_of(db), user_id=user_id, opted_out_at=now()))
    elif not opted_out and row is not None:
        await db.delete(row)
    await db.flush()
    from app.services.calendar_sync import org_free_busy

    await org_free_busy.clear_cache(tenant_of(db))
    return await mine(ctx, db)


async def _interview(db: AsyncSession, interview_id: uuid.UUID) -> Interview:
    row = await db.get(Interview, interview_id)
    if row is None:
        raise NotFound("Interview not found")
    return row


async def sync_health(db: AsyncSession, interview_id: uuid.UUID) -> list[ParticipantSyncState]:
    row = await _interview(db, interview_id)
    parts = list(
        (await db.execute(select(InterviewParticipant).where(InterviewParticipant.interview_id == row.id, InterviewParticipant.removed_at.is_(None)))).scalars()
    )
    people: dict[uuid.UUID, uuid.UUID | None] = {row.organizer_user_id: None}
    for p in parts:
        if p.user_id is not None and p.kind in ("interviewer_internal", "observer"):
            people.setdefault(p.user_id, p.id)
        if p.user_id == row.organizer_user_id:
            people[row.organizer_user_id] = p.id
    out: list[ParticipantSyncState] = []
    for user_id, pid in people.items():
        conn = (
            await db.execute(select(UserConnection).where(UserConnection.user_id == user_id, UserConnection.status != "revoked").order_by(UserConnection.connected_at.desc()).limit(1))
        ).scalar_one_or_none()
        if conn is None:
            state = "not_connected"
        elif conn.status != "active":
            state = "not_syncing"
        else:
            ch = (
                await db.execute(select(CalendarChannel).where(CalendarChannel.connection_id == conn.id).order_by(CalendarChannel.created_at.desc()).limit(1))
            ).scalar_one_or_none()
            if ch is None:
                state = "not_connected" if user_id != row.organizer_user_id else "syncing"
            elif ch.status in ("active", "renewing"):
                state = "syncing"
            elif ch.status == "polling":
                state = "polling"
            else:
                state = "not_syncing"
        out.append(ParticipantSyncState(participant_id=pid, user_id=user_id, name=await user_name(db, user_id), state=state))  # type: ignore[arg-type]
    return out


async def interview_sync(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> InterviewSyncOut:
    from app.services import interviews_service

    await interviews_service.get_interview(ctx, db, interview_id)  # readability check (404 otherwise)
    row = await _interview(db, interview_id)
    settings = await settings_row(db)
    organiser = await user_name(db, row.organizer_user_id)
    link_rows = await links.for_interview(db, interview_id)
    return InterviewSyncOut(
        interview_id=interview_id, move_policy=settings.move_policy,  # type: ignore[arg-type]
        participants=await sync_health(db, interview_id),
        links=[
            EventLinkOut(
                id=lk.id, role=lk.role, participant_id=lk.participant_id, state=lk.state,  # type: ignore[arg-type]
                seen_start=lk.seen_start, seen_end=lk.seen_end, organizer_name=organiser if lk.role == "organizer" else None,
            )
            for lk in link_rows
        ],
    )


async def recreate_event(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> str:
    from app.services import interviews_service

    await interviews_service.get_interview(ctx, db, interview_id)
    event_id = await interviews_service.resync_event(db, interview_id, reason="calendar_recreate", recreate=True)
    audit.record("calendar_sync.event_recreated", "interview", interview_id, session=db)
    return "recreated" if event_id else "unavailable"


async def keep_time(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> str:
    from app.services import interviews_service
    from app.services.calendar_sync import rules

    await interviews_service.get_interview(ctx, db, interview_id)
    n = await rules.keep_talent_os_time(db, interview_id)
    audit.record("calendar_sync.kept_talent_os_time", "interview", interview_id, session=db)
    return "restored" if n else "nothing_to_do"


async def dismiss(ctx: RequestContext, db: AsyncSession, interview_id: uuid.UUID) -> str:
    from app.services import interviews_service

    await interviews_service.get_interview(ctx, db, interview_id)
    count = 0
    for lk in await links.for_interview(db, interview_id):
        if lk.state in ("deleted", "moved", "conflict"):
            lk.state = "detached"
            count += 1
    await db.flush()
    audit.record("calendar_sync.change_dismissed", "interview", interview_id, session=db, links=count)
    return "dismissed"


async def list_channels(ctx: RequestContext, db: AsyncSession, status: str | None) -> list[ChannelAdminOut]:  # noqa: ARG001
    stmt = select(CalendarChannel).order_by(CalendarChannel.created_at.desc()).limit(500)
    if status:
        stmt = stmt.where(CalendarChannel.status == status)
    rows = list((await db.execute(stmt)).scalars())
    return [
        ChannelAdminOut(
            id=r.id, person=await user_name(db, r.user_id), provider=r.provider, status=r.status,  # type: ignore[arg-type]
            last_notification_at=r.last_notification_at, last_sync_at=r.last_sync_at,
        )
        for r in rows
    ]
