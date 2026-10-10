# ruff: noqa: E501
"""Registrations of the ``legal_entities`` domain (autodiscovered by ``core.hooks``).

* Tenant provisioning: the default Legal Entity (+ open periods) for every new tenant (AD-17).

Close checks are registered by the domains that own the checked records (Epics 25-30).
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.provisioning import register_tenant_provisioner


async def _provision(db: AsyncSession, tenant: Any) -> None:
    from app.services.legal_entities.service import provision_default_entity

    await provision_default_entity(db, tenant)


register_tenant_provisioner("legal_entities.default_entity", _provision, order=10)


# ---------------------------------------------------------------- Epic 51 (FR-255): live FX feed alerts + settings
async def _fx_admins(db: AsyncSession, payload: Any) -> set[Any]:  # noqa: ARG001
    from app.services import identity_bridge

    return {u["id"] for u in await identity_bridge.users_with_permission(db, "fx_feed:configure")}


async def _render_fx_failed(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    from app.services.notifications_service import Content

    days = int(payload.get("consecutive_days") or 1)
    tail = "use the latest feed rate (up to the staleness limit) or record a manual rate"
    return Content(
        title=f"FX feed failed on {payload.get('on_date')} ({days} day(s) in a row) -- {tail}"[:300],
        link_path="/settings/fx-feed",
    )


def _register_fx_feed() -> None:
    from app.events.legal_entities import FxFeedFailed
    from app.services.notifications_service import (
        NOTIFICATION_TYPES,
        NotificationType,
        register_event_source,
        register_notification_type,
    )
    from app.services.settings_service import register_settings_entry

    if "fx_feed_failed" not in NOTIFICATION_TYPES:
        register_notification_type(
            "fx_feed_failed",
            NotificationType("FX feed failed", "Finance", "both", False, _fx_admins, _render_fx_failed, internal_only=True),
        )
        register_event_source(FxFeedFailed, "fx_feed_failed")
    register_settings_entry(
        "finance", "fx_feed", "FX feed", "Daily reference FX rates, precedence and staleness.",
        "/api/v1/fx-feed/settings", "fx_feed:configure", owner_epic="Epic 51",
    )
    register_settings_entry(
        "finance", "dsc_signing", "DSC signing", "Digital-signature (DSC) requirements per GST registration and Client.",
        "/api/v1/dsc/policies", "dsc:configure", owner_epic="Epic 51",
    )


try:
    _register_fx_feed()
except Exception:  # pragma: no cover - never block other domains' registrations
    from loguru import logger

    logger.exception("legal_entities registration _register_fx_feed failed")
