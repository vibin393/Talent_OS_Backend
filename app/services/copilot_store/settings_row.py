# ruff: noqa: E501
"""Tenant Copilot settings row (Epic 54, ``copilot_settings``) and the availability inputs of ``status`` (features,
kill switches, monthly AI allowance). Defaults apply until an administrator saves the settings."""

import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.copilot import (
    DEFAULT_MAX_OPEN_CONVERSATIONS,
    DEFAULT_QUESTIONS_PER_HOUR,
    DEFAULT_RETENTION_DAYS,
    CopilotSettings,
)


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    retention_days: int = DEFAULT_RETENTION_DAYS
    questions_per_hour: int = DEFAULT_QUESTIONS_PER_HOUR
    max_open_conversations: int = DEFAULT_MAX_OPEN_CONVERSATIONS
    allow_feedback_comments: bool = True
    updated_at: datetime | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Availability:
    features: dict[str, bool]
    tenant_kill: bool
    global_kill: bool
    budget_exhausted: bool


async def get(db: AsyncSession, tenant_id: uuid.UUID) -> Settings:
    row = await db.get(CopilotSettings, tenant_id)
    if row is None:
        return Settings()
    return Settings(
        retention_days=row.retention_days, questions_per_hour=row.questions_per_hour,
        max_open_conversations=row.max_open_conversations, allow_feedback_comments=row.allow_feedback_comments,
        updated_at=row.updated_at,
    )  # fmt: skip


async def update(
    db: AsyncSession, tenant_id: uuid.UUID, changes: dict[str, Any], updated_by: uuid.UUID | None
) -> Settings:
    row = await db.get(CopilotSettings, tenant_id)
    if row is None:
        row = CopilotSettings(
            tenant_id=tenant_id, retention_days=DEFAULT_RETENTION_DAYS, questions_per_hour=DEFAULT_QUESTIONS_PER_HOUR,
            max_open_conversations=DEFAULT_MAX_OPEN_CONVERSATIONS, allow_feedback_comments=True,
        )  # fmt: skip
        db.add(row)
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_by = updated_by
    row.updated_at = datetime.now(UTC)
    audit.record(
        "copilot.settings_changed", "copilot_settings", tenant_id, session=db, fields=sorted(changes)
    )
    await db.flush()
    return await get(db, tenant_id)


async def availability(ctx: Any, db: AsyncSession, tenant_id: uuid.UUID) -> Availability:
    from app.services import agent_runtime_service as runtime
    from app.services import ai
    from app.services.ai import budget as budget_guard

    state = await runtime.agentic_state(ctx, db)
    features = await ai.features_map(db)
    monthly = await ai.monthly_budget(db)
    exhausted = False
    if monthly is not None:
        exhausted = budget_guard.state_for(await budget_guard.usage(db, tenant_id), monthly).all_blocked
    return Availability(
        features=dict(features),
        tenant_kill=bool(state.kill_switch or state.platform_kill_switch),
        global_kill=bool(state.global_kill_switch),
        budget_exhausted=exhausted,
    )


__all__ = ["Availability", "Settings", "availability", "get", "update"]
