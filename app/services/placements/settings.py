"""Tenant placement settings (Epic 19 A1): one row per tenant, created lazily. ``human_id_prefix`` is immutable once
a Placement exists."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.models.placements import Placement
from app.schemas.placements_api import PlacementSettingsOut, PlacementSettingsUpdate
from app.services.placements.common import settings_row, tenant_of

_RULE_KEYS = {"no_response_touchpoints", "onboarding_pct_at_t7", "counter_offer"}


async def get_settings(ctx: RequestContext, db: AsyncSession) -> PlacementSettingsOut:
    return PlacementSettingsOut.model_validate(await settings_row(db, tenant_of(ctx, db)))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: PlacementSettingsUpdate
) -> PlacementSettingsOut:
    row = await settings_row(db, tenant_of(ctx, db))
    changes: dict[str, Any] = data.model_dump(exclude_unset=True)
    if "human_id_prefix" in changes and changes["human_id_prefix"] != row.human_id_prefix:
        exists = (await db.execute(select(Placement.id).limit(1))).first()
        if exists:
            raise ConflictError(
                "The placement ID prefix cannot change once placements exist", code="prefix_locked"
            )
    if "dropout_rules" in changes and changes["dropout_rules"] is not None:
        unknown = set(changes["dropout_rules"]) - _RULE_KEYS
        if unknown:
            raise ValidationFailed(
                "Unknown dropout rule", code="unknown_rule", details={"rules": sorted(unknown)}
            )
    if changes.get("touchpoint_email_template"):
        from jinja2 import TemplateSyntaxError
        from jinja2.sandbox import SandboxedEnvironment

        try:
            SandboxedEnvironment(autoescape=True).parse(changes["touchpoint_email_template"])
        except TemplateSyntaxError as exc:
            raise ValidationFailed("The email template is invalid", code="invalid_template") from exc
    for key, value in changes.items():
        if value is None and key not in ("touchpoint_email_template", "min_margin_pct"):
            continue
        if key == "touchpoint_offsets":
            value = sorted(set(value), reverse=True)
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return PlacementSettingsOut.model_validate(row)
