# ruff: file-ignore[line-too-long]
"""Calendar Sync wiring (Epic 72), autodiscovered by ``core.hooks``.

* the calendar Connector Manifests (listing-only personal + organisation free/busy);
* the P1 registries (D-18): tag provider + event-written listener (``interviews.invites`` via ``connections_service``),
  connection listener (connected / needs_reconnect / revoked) and the organisation free/busy fallback;
* after-commit handlers: ``InterviewScheduled`` / ``InterviewRescheduled`` and ``CalendarSyncKick`` -> ensure channels;
* notification types ``calendar_change_request``, ``calendar_not_syncing``, ``org_free_busy_enabled`` (mandatory);
* the settings hub entry and the audit labels.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession


def _manifests() -> None:
    from app.core.integrations import register_connector
    from app.services.connections.manifests import CALENDAR_MANIFESTS

    for m in CALENDAR_MANIFESTS:
        register_connector(m)


def _p1_hooks() -> None:
    from app.services import connections_service
    from app.services.calendar_sync import links, org_free_busy

    async def on_connection(db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID, status: str) -> None:
        from app.core.events import emit
        from app.events.calendar_sync import CalendarSyncKick
        from app.services.calendar_sync import channels
        from app.services.calendar_sync.common import tenant_of

        if status == "active":
            emit(db, CalendarSyncKick(tenant_id=tenant_of(db), connection_id=connection_id))
        else:
            await channels.stop_for_connection(db, connection_id, user_id, status)

    connections_service.set_event_tag_provider(links.make_tag)
    connections_service.register_event_written_listener(links.on_event_written)
    connections_service.register_connection_listener(on_connection)
    connections_service.register_free_busy_fallback(org_free_busy.lookup)


def _handlers() -> None:
    from app.core.events import subscribe
    from app.events.calendar_sync import CalendarSyncKick
    from app.events.interviews import InterviewRescheduled, InterviewScheduled

    def kick(event: Any) -> None:
        from app.tasks import calendar_sync as tasks
        from app.tasks.runtime import enqueue

        if isinstance(event, CalendarSyncKick):
            if event.backfill:
                enqueue(tasks.backfill, str(event.tenant_id))
            elif event.connection_id is not None:
                enqueue(tasks.ensure_channel, str(event.tenant_id), str(event.connection_id))
            elif event.interview_id is not None:
                enqueue(tasks.ensure_for_interview, str(event.tenant_id), str(event.interview_id))
            return
        enqueue(tasks.ensure_for_interview, str(event.tenant_id), str(event.interview_id))

    subscribe(InterviewScheduled, kick)
    subscribe(InterviewRescheduled, kick)
    subscribe(CalendarSyncKick, kick)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _notifications() -> None:
    from app.services import notifications_service as ns

    async def to_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]  # NOSONAR
        uid = _uuid(payload.get("user_id"))
        return {uid} if uid else set()

    async def everyone(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        from sqlalchemy import select

        from app.models.identity import User

        return set((await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars())

    async def change(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        iid = _uuid(payload.get("interview_id"))
        return ns.Content(
            title=str(payload.get("title") or "A calendar change needs you"),
            body="A calendar change affects a scheduled interview. The interview itself has not changed.",
            entity_type=None, entity_id=None, link_path=f"/interviews/{iid}" if iid else None,
        )  # fmt: skip

    async def not_syncing(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title="Reconnect your calendar so interview changes reach Talent OS",
            body="Your calendar connection stopped working. Changes you make in your calendar won't reach Talent OS.",
            entity_type=None, entity_id=None, link_path="/settings/connections",
        )  # fmt: skip

    async def org_enabled(db: AsyncSession, payload: Any) -> Any:
        from app.models.identity import Tenant

        tenant = await db.get(Tenant, _uuid(payload.get("tenant_id")))
        name = getattr(tenant, "name", None) or "Your organisation"
        return ns.Content(
            title=f"{name} now shares your calendar free/busy with Talent OS",
            body=(
                f"{name} now shares your calendar free/busy times (not event details) with Talent OS for interview "
                "scheduling. You can opt out in Settings → Connections."
            ),
            entity_type=None, entity_id=None, link_path="/settings/connections",
        )  # fmt: skip

    for key, label, channel, mandatory, recipients, render in (
        ("calendar_change_request", "Calendar change on an interview", "in_app", False, to_user, change),
        ("calendar_not_syncing", "Calendar not syncing", "in_app", False, to_user, not_syncing),
        ("org_free_busy_enabled", "Organisation free/busy enabled", "both", True, everyone, org_enabled),
    ):
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(
                key, ns.NotificationType(label, "Integrations", channel, mandatory, recipients, render, internal_only=True)
            )


def _settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "integrations", "calendar_sync", "Calendar sync", "Bring interview changes back from Google and Microsoft calendars.",
        "/api/v1/calendar-sync/settings", "integration:manage", owner_epic="Epic 72",
    )  # fmt: skip


def _audit() -> None:
    from app.core import audit

    for key, text in (
        ("calendar_sync.settings_updated", "Calendar sync settings were changed"),
        ("calendar_sync.channel_created", "A calendar push channel was created"),
        ("calendar_sync.channel_stopped", "A calendar push channel was stopped"),
        ("calendar_sync.event_recreated", "An interview calendar event was re-created"),
        ("calendar_sync.kept_talent_os_time", "An interview calendar event was put back to the Talent OS time"),
        ("calendar_sync.change_dismissed", "A calendar change on an interview was dismissed"),
        ("scheduling_request.calendar_change", "A calendar change asked for a reschedule"),
    ):
        audit.register_audit_action(key, text)


for _fn in (_manifests, _p1_hooks, _handlers, _notifications, _settings, _audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("calendar sync hooks: {} failed", _fn.__name__)
        raise
