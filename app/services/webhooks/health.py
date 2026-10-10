# ruff: noqa: E501
"""Subscription health (Epic 68, §0.8, FR-352): ``webhooks.health_scan`` (every 5 min) notifies a 1 h failure
streak once per streak, auto-disables a 72 h streak with no success (open deliveries become ``held``), announces the
end of a rotation overlap 2 h ahead and resets rows stuck ``in_flight`` (worker crash)."""

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.webhooks import WebhookSubscription
from app.services.webhooks import common
from app.services.webhooks.scheduler import reset_stuck
from app.services.webhooks.subscriptions import changed, disable

SCAN_SECONDS = 300
OVERLAP_HOURS = 24
EXPIRY_NOTICE_HOURS = 2


async def scan_db(db: AsyncSession) -> int:
    s = get_settings()
    now = common.now()
    touched = 0
    subs = list(
        (await db.execute(select(WebhookSubscription).where(WebhookSubscription.deleted_at.is_(None), WebhookSubscription.status == "active").with_for_update(skip_locked=True))).scalars()
    )
    for sub in subs:
        start = sub.failure_streak_started_at
        no_success_since = sub.last_success_at is None or (start is not None and sub.last_success_at < start)
        if start is not None and no_success_since:
            age = (now - start).total_seconds()
            if age >= s.webhook_auto_disable_after_seconds:
                await disable(db, sub, "failing")
                touched += 1
                continue
            if age >= s.webhook_failure_notify_after_seconds and sub.streak_notified_at is None:
                sub.streak_notified_at = now
                changed(db, sub, "failing")
                touched += 1
        rotated = sub.secret_rotated_at
        if rotated is not None:
            notice_at = rotated + timedelta(hours=OVERLAP_HOURS - EXPIRY_NOTICE_HOURS)
            if notice_at <= now < notice_at + timedelta(seconds=SCAN_SECONDS):
                changed(db, sub, "secret_expiring")
                touched += 1
    return touched


async def scan(tenant_id: uuid.UUID) -> int:
    reset = await reset_stuck(tenant_id)
    async with tenant_session(tenant_id) as db:
        return reset + await scan_db(db)


__all__ = ["scan", "scan_db"]
