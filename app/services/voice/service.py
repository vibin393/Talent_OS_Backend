# ruff: noqa: E501
"""Voice screening tenant settings (Epic 58, Part C): default call method (``manual``; ``fake`` refused in
production), consent link validity (1..14 days), consent channel order and the recording upload limit (<= 200 MB)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.settings import get_settings
from app.models.voice import VoiceSettings
from app.schemas.voice import VoiceSettingsIn, VoiceSettingsOut
from app.services.voice import common


def _fake_available() -> bool:
    from app.services.connectors.registry import adapter_factory

    return get_settings().app_env != "production" and adapter_factory("voice", "fake") is not None


def _out(row: VoiceSettings) -> VoiceSettingsOut:
    return VoiceSettingsOut(
        default_provider=row.default_provider,  # type: ignore[arg-type]
        consent_link_days=row.consent_link_days,
        consent_channel_order=list(row.consent_channel_order or []),
        max_upload_mb=row.max_upload_mb,
        fake_available=_fake_available(),
    )


async def get_settings_out(ctx: RequestContext, db: AsyncSession) -> VoiceSettingsOut:  # noqa: ARG001
    return _out(await common.settings_row(db))


async def update_settings(ctx: RequestContext, db: AsyncSession, data: VoiceSettingsIn) -> VoiceSettingsOut:  # noqa: ARG001
    row = await common.settings_row(db)
    if data.default_provider == "fake" and not _fake_available():
        raise common.invalid("provider_not_available", "Demo calls are not available here")
    before = _out(row).model_dump(exclude={"fake_available"})
    for field, value in data.model_dump(exclude_none=True).items():
        if field == "consent_channel_order":
            value = list(dict.fromkeys(value))
        setattr(row, field, value)
    await db.flush()
    after = _out(row)
    audit.record(
        "voice_settings.changed",
        "voice_settings",
        row.tenant_id,
        session=db,
        before=before,
        after=after.model_dump(exclude={"fake_available"}),
    )
    return after


__all__ = ["get_settings_out", "update_settings"]
