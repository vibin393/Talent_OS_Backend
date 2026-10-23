"""Pipeline settings (A1): one row per tenant, created lazily. Secure-link expiries are read from the tenant
settings hub (``settings_service.link_ttl``, the single source of link TTLs) and shown read-only here."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.pipeline import PipelineSettings
from app.schemas.pipeline import PipelineSettingsOut, PipelineSettingsUpdate
from app.services.pipeline.common import (
    DEFAULT_BURDEN,
    DEFAULT_REJECTION_BODY,
    DEFAULT_REJECTION_SUBJECT,
    settings_row,
)

LINK_PURPOSES = ("ai_screening", "client_feedback", "consent_request")


def burden_for(row: PipelineSettings, employment_type: str | None) -> Decimal:
    table = {**DEFAULT_BURDEN, **(row.burden_pct or {})}
    return Decimal(str(table.get(employment_type or "", 0)))


async def settings_out(db: AsyncSession, row: PipelineSettings) -> PipelineSettingsOut:
    from app.services import settings_service

    link_days = {p: (await settings_service.link_ttl(db, p)).days for p in LINK_PURPOSES}
    return PipelineSettingsOut(
        submission_consent_mode=row.submission_consent_mode,
        prior_submission_window_days=row.prior_submission_window_days,
        min_margin_pct=Decimal(row.min_margin_pct),
        burden_pct={k: Decimal(str(v)) for k, v in {**DEFAULT_BURDEN, **(row.burden_pct or {})}.items()},
        rejection_email_enabled=row.rejection_email_enabled,
        rejection_email_subject=row.rejection_email_subject or DEFAULT_REJECTION_SUBJECT,
        rejection_email_body=row.rejection_email_body or DEFAULT_REJECTION_BODY,
        auto_interview_on_interest=row.auto_interview_on_interest,
        show_bill_rate_in_submission=row.show_bill_rate_in_submission,
        link_days=link_days,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> PipelineSettingsOut:  # ruff: ignore[unused-function-argument]
    return await settings_out(db, await settings_row(db))


async def update_settings(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    data: PipelineSettingsUpdate,
) -> PipelineSettingsOut:
    row = await settings_row(db)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key not in ("rejection_email_subject", "rejection_email_body"):
            continue
        if key == "burden_pct":
            row.burden_pct = {**(row.burden_pct or {}), **{k: float(v) for k, v in value.items()}}
        else:
            setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return await settings_out(db, row)
