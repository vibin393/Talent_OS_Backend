# ruff: file-ignore[line-too-long]
"""Organisation free/busy source (Epic 72 story 72.4, spec A7), registered as the ``connections_service`` free/busy
fallback. Only for users without a usable personal connection who did not opt out; free/busy only (never event
details); the installed ``google_calendar_org`` / ``microsoft_calendar_org`` connector is reached through
``integrations_service.adapter_for`` (sandbox -> Fake, Egress Guard, vault secrets)."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.calendar_sync import CalendarFreeBusyOptout
from app.services.calendar_sync.common import settings_row, system_ctx
from app.services.calendar_sync.settings import ORG_KEYS
from app.services.connections.provider import BusyBlock

Source = Callable[[AsyncSession, list[str], datetime, datetime], Awaitable[dict[str, list[tuple[datetime, datetime]]]]]
_override: list[Source] = []
RUNNABLE = ("active", "degraded", "reconsent_required")


def set_source(fn: Source | None) -> None:
    """Replace (tests) the installed connector with ``fn`` (None restores it)."""
    _override.clear()
    if fn is not None:
        _override.append(fn)


async def _installed_source(db: AsyncSession, emails: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    from app.models.oauth import AppInstallation
    from app.services import integrations_service

    inst = (
        await db.execute(
            select(AppInstallation).where(AppInstallation.connector_key.in_(ORG_KEYS), AppInstallation.status.in_(RUNNABLE)).limit(1)
        )
    ).scalar_one_or_none()
    if inst is None:
        return {}
    adapter = await integrations_service.adapter_for(system_ctx(inst.tenant_id), db, inst.id)
    data = await adapter.handle("free_busy", {"emails": emails, "start": start.isoformat(), "end": end.isoformat()})
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    for email, blocks in dict(data.get("busy") or {}).items():
        out[str(email).lower()] = [
            (datetime.fromisoformat(str(b[0]).replace("Z", "+00:00")), datetime.fromisoformat(str(b[1]).replace("Z", "+00:00")))
            for b in blocks
        ]
    return out


async def lookup(
    db: AsyncSession, tenant_id: uuid.UUID, user_ids: list[uuid.UUID], start: datetime, end: datetime  # ruff: ignore[unused-function-argument]
) -> dict[uuid.UUID, list[BusyBlock]]:
    from app.models.identity import User
    from app.services import connections_service

    settings = await settings_row(db)
    if not settings.org_source_enabled or not user_ids:
        return {}
    opted = set((await db.execute(select(CalendarFreeBusyOptout.user_id).where(CalendarFreeBusyOptout.user_id.in_(user_ids)))).scalars())
    emails: dict[str, uuid.UUID] = {}
    for uid in user_ids:
        if uid in opted or await connections_service.has_active(db, uid):
            continue
        user = await db.get(User, uid)
        if user is not None and getattr(user, "email", None):
            emails[str(user.email).lower()] = uid
    if not emails:
        return {}
    source = _override[0] if _override else _installed_source
    try:
        found = await source(db, list(emails), start, end)
    except Exception as exc:
        logger.warning("Organisation free/busy failed (error={})", type(exc).__name__)
        return {}
    out: dict[uuid.UUID, list[BusyBlock]] = {}
    for email, uid in emails.items():
        if email in found:
            out[uid] = [BusyBlock(s, e) for s, e in found[email]]
    return out


async def clear_cache(tenant_id: uuid.UUID) -> None:
    """Let the P1 free/busy cache (2 minutes) and has no per-tenant listing; entries age out (documented)."""
    logger.info("calendar sync: organisation free/busy changed for tenant {}; cached entries expire within 2 minutes", tenant_id)
