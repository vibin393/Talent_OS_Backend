# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared helpers of the ``scheduling_agent`` domain (Epic 59): constants, clock, the tenant settings row, contexts,
gates and errors."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import Principal
from app.models.scheduling_agent import DEFAULT_WORKING_HOURS, SchedulingAgentSettings, SchedulingRequest

AGENT_KEY = "scheduling"
FEATURE = "agent_scheduling"
LINK_PURPOSE = "slot_pick"
LINK_SUBJECT = "scheduling_slot_offer"
FREE_BUSY = "calendar:free_busy"
SCHEDULE = "interview:schedule"
READ = "interview:read"
CONFIGURE = "ai:agent_configure"
LINK_MAX_USES = 100
LEAD_MINUTES = 120  # the earliest slot starts two hours from now
REMINDER_GRACE_MINUTES = 10  # P1 sends a reminder the agent has not sent once it is this overdue (fallback)


def _system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _system_clock


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """Tests: replace the scheduling clock (None restores it)."""
    global _clock  # process-wide test hook
    _clock = clock or _system_clock


def now() -> datetime:
    return _clock()


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "scheduling_agent needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def principal_of(ctx: RequestContext) -> Principal | None:
    p = ctx.principal
    return p if isinstance(p, Principal) else None


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    p = principal_of(ctx)
    return p.id if p is not None and p.type == "user" else None


def as_user(ctx: RequestContext) -> RequestContext:
    """A context whose principal is a ``user`` (P1 free/busy and visibility checks accept only users). Agent contexts
    become the Accountable Owner with the Effective Rights (actor stays ``agent``; ``scope_context``)."""
    p = principal_of(ctx)
    if p is not None and p.type == "agent" and ctx.on_behalf_of is not None:
        from app.services import agent_runtime_service

        return agent_runtime_service.scope_context(ctx, ctx.on_behalf_of)
    return ctx


def require(ctx: RequestContext, *permissions: str) -> None:
    from app.core.permissions import check

    p = principal_of(ctx)
    if p is None:
        return  # system contexts (tasks) are trusted callers
    for perm in permissions:
        check(p, perm)


def actor_of(ctx: RequestContext) -> tuple[str, uuid.UUID | None]:
    from app.core.actors import actor_from_ctx

    actor = actor_from_ctx(ctx)
    return actor.kind, actor.id


def system_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


async def settings_row(db: AsyncSession) -> SchedulingAgentSettings:
    """The tenant's settings row (created lazily with the defaults)."""
    row = (await db.execute(select(SchedulingAgentSettings))).scalar_one_or_none()
    if row is not None:
        return row
    await db.execute(
        pg_insert(SchedulingAgentSettings)
        .values(id=uuid.uuid4(), tenant_id=tenant_of(db), default_working_hours=DEFAULT_WORKING_HOURS)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    return (await db.execute(select(SchedulingAgentSettings))).scalar_one()


async def request_row(
    db: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> SchedulingRequest:
    stmt = select(SchedulingRequest).where(SchedulingRequest.id == request_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Scheduling request not found")
    return row


async def agent_on(db: AsyncSession) -> bool:
    """The ``agentic`` master switch and ``agent_scheduling`` are on (kill switches are checked by ``agent_active``)."""
    from app.services import ai

    features = await ai.features_map(db)
    return bool(features.get("agentic")) and bool(features.get(FEATURE))


async def agent_active(db: AsyncSession, trigger_key: str | None = None) -> bool:
    """On, not killed, and (when given) the trigger enabled -- the reminder owner hook and the beat use this."""
    from app.models.agent_runtime import AgentConfig
    from app.services.agent_runtime import state

    gates = await state.gates(db)
    if gates.any_kill or not gates.agentic_on or not gates.agent_on(AGENT_KEY):
        return False
    if trigger_key is None:
        return True
    config = (
        await db.execute(select(AgentConfig).where(AgentConfig.agent_key == AGENT_KEY))
    ).scalar_one_or_none()
    return not (config is not None and (config.triggers_enabled or {}).get(trigger_key) is False)


__all__ = [
    "AGENT_KEY",
    "CONFIGURE",
    "FEATURE",
    "FREE_BUSY",
    "LEAD_MINUTES",
    "LINK_MAX_USES",
    "LINK_PURPOSE",
    "LINK_SUBJECT",
    "READ",
    "REMINDER_GRACE_MINUTES",
    "SCHEDULE",
    "actor_of",
    "agent_active",
    "agent_on",
    "as_user",
    "conflict",
    "invalid",
    "now",
    "principal_of",
    "request_row",
    "require",
    "set_clock",
    "settings_row",
    "system_ctx",
    "tenant_of",
    "user_id_of",
]
