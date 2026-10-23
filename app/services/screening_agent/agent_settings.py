"""Tenant settings of the Screening Agent (Epic 57 A8): ``GET/PUT /settings/agents/screening``."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.schemas.screening_agent import ScreeningAgentSettingsIn, ScreeningAgentSettingsOut
from app.services.screening_agent.common import DEFAULT_STOP_WORDS, settings_row, user_id_of


def _out(row: Any) -> ScreeningAgentSettingsOut:
    return ScreeningAgentSettingsOut(
        expiry_days=row.expiry_days,
        reminder_after_hours=row.reminder_after_hours,
        max_reminders=row.max_reminders,
        questions_per_turn=row.questions_per_turn,
        low_confidence_threshold=row.low_confidence_threshold,
        stop_words=list(row.stop_words or DEFAULT_STOP_WORDS),
        whatsapp_invite_template_id=row.whatsapp_invite_template_id,
        default_channel=row.default_channel,
        allow_campus_candidates=row.allow_campus_candidates,
    )


async def get(ctx: RequestContext, db: AsyncSession) -> ScreeningAgentSettingsOut:  # ruff: ignore[unused-function-argument]
    return _out(await settings_row(db))


async def update(
    ctx: RequestContext, db: AsyncSession, data: ScreeningAgentSettingsIn
) -> ScreeningAgentSettingsOut:
    row = await settings_row(db)
    values = data.model_dump(exclude_unset=True)
    template_id = values.get("whatsapp_invite_template_id")
    if template_id is not None:
        from app.models.comms import ChannelTemplate

        template = await db.get(ChannelTemplate, uuid.UUID(str(template_id)))
        if template is None or template.channel != "whatsapp":
            raise NotFound("WhatsApp template not found")
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_by = user_id_of(ctx)
    await db.flush()
    return _out(row)


__all__ = ["get", "update"]
