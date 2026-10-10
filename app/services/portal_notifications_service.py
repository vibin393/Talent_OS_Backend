"""Portal notification inbox of External Principals (Epic 35 A7, FR-184, AD-26).

One service behind three mounts (Client Portal, Vendor Portal, Worker "My Work"). Every query filters on
``recipient_kind`` + ``recipient_id`` = the calling principal; the portal session's org RLS applies on top
(fail-closed), so another Client's / Vendor's / Worker's notification id is simply not found (404).
Preferences: an email on/off switch per type (mandatory types cannot be switched off), stored in
``portal_notification_preferences``.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ValidationFailed
from app.models.notifications import Notification
from app.models.portal_notifications import PortalNotificationPreference

PAGE_LIMIT = 50


class PortalNotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str | None
    link: str | None
    read_at: datetime | None
    created_at: datetime


class PortalInboxOut(BaseModel):
    items: list[PortalNotificationOut]
    unread_count: int


class PortalPreferenceOut(BaseModel):
    type: str
    label: str
    email: bool
    mandatory: bool


class PortalPreferenceIn(BaseModel):
    type: str
    email: bool


def _mine(kind: str, recipient_id: uuid.UUID) -> tuple[Any, ...]:
    return (
        Notification.recipient_kind == kind,
        Notification.recipient_id == recipient_id,
        Notification.in_app.is_(True),
    )


def _out(n: Notification) -> PortalNotificationOut:
    return PortalNotificationOut(
        id=n.id,
        type=n.type,
        title=n.title,
        body=n.body,
        link=n.link_path,
        read_at=n.read_at,
        created_at=n.created_at,
    )


async def _unread(db: AsyncSession, kind: str, recipient_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Notification)
                .where(*_mine(kind, recipient_id), Notification.read_at.is_(None))
            )
        ).scalar_one()
    )


async def inbox(
    db: AsyncSession, kind: str, recipient_id: uuid.UUID, *, unread_only: bool = False
) -> PortalInboxOut:
    stmt = select(Notification).where(*_mine(kind, recipient_id))
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = (
        await db.execute(
            stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(PAGE_LIMIT)
        )
    ).scalars()
    return PortalInboxOut(items=[_out(n) for n in rows], unread_count=await _unread(db, kind, recipient_id))


async def mark_read(
    db: AsyncSession, kind: str, recipient_id: uuid.UUID, notification_id: uuid.UUID
) -> PortalNotificationOut:
    row = (
        await db.execute(
            select(Notification).where(Notification.id == notification_id, *_mine(kind, recipient_id))
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await db.flush()
    return _out(row)


async def mark_all_read(db: AsyncSession, kind: str, recipient_id: uuid.UUID) -> PortalInboxOut:
    await db.execute(
        update(Notification)
        .where(*_mine(kind, recipient_id), Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    return await inbox(db, kind, recipient_id)


def _portal_types() -> dict[str, Any]:
    from app.services.notifications_service import NOTIFICATION_TYPES

    return {
        k: d
        for k, d in NOTIFICATION_TYPES.items()
        if getattr(d, "external", False) or getattr(d, "portal_path", None)
    }


async def preferences(db: AsyncSession, kind: str, recipient_id: uuid.UUID) -> list[PortalPreferenceOut]:
    from app.services.notifications_service import NOTIFICATION_TYPES

    received = set(
        (await db.execute(select(Notification.type).where(*_mine(kind, recipient_id)).distinct())).scalars()
    )
    types = {**_portal_types(), **{k: NOTIFICATION_TYPES[k] for k in received if k in NOTIFICATION_TYPES}}
    stored = {
        r.type: r.email
        for r in (
            await db.execute(
                select(PortalNotificationPreference).where(
                    PortalNotificationPreference.recipient_kind == kind,
                    PortalNotificationPreference.recipient_id == recipient_id,
                )
            )
        ).scalars()
    }
    return [
        PortalPreferenceOut(
            type=key,
            label=d.label,
            email=True if d.mandatory_in_app else stored.get(key, True),
            mandatory=d.mandatory_in_app,
        )
        for key, d in sorted(types.items())
    ]


async def set_preference(
    db: AsyncSession, kind: str, recipient_id: uuid.UUID, data: PortalPreferenceIn
) -> list[PortalPreferenceOut]:
    from app.services.notifications_service import NOTIFICATION_TYPES

    definition = NOTIFICATION_TYPES.get(data.type)
    if definition is None:
        raise ValidationFailed("Unknown notification type", details={"fields": {"type": "Unknown type"}})
    if definition.mandatory_in_app and not data.email:
        raise ValidationFailed("This notification cannot be switched off", code="mandatory_notification")
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    row = (
        await db.execute(
            select(PortalNotificationPreference).where(
                PortalNotificationPreference.recipient_kind == kind,
                PortalNotificationPreference.recipient_id == recipient_id,
                PortalNotificationPreference.type == data.type,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            PortalNotificationPreference(
                id=uuid.uuid4(), tenant_id=tenant_id, recipient_kind=kind, recipient_id=recipient_id, type=data.type,
                email=data.email,
            )
        )  # fmt: skip
    else:
        row.email = data.email
    await db.flush()
    return await preferences(db, kind, recipient_id)
