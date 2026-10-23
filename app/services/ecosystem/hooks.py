# ruff: file-ignore[line-too-long]
"""Ecosystem registrations (Epic 66, spec 0.17): notification types for the Ecosystem Kill Switch (mandatory),
Installation changes, re-consent, connector sync failures / reconnects and platform App suspensions."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def _holders(db: AsyncSession, *permissions: str) -> set[uuid.UUID]:
    rows = await db.execute(
        text(
            "SELECT DISTINCT ur.user_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
            "JOIN users u ON u.id = ur.user_id WHERE u.is_active AND (r.key = 'tenant_admin' "
            "OR EXISTS (SELECT 1 FROM jsonb_object_keys(r.permissions) k WHERE k = ANY(:perms)))"
        ),
        {"perms": list(permissions)},
    )
    return {r[0] for r in rows}


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _register() -> None:
    from app.events.ecosystem import (
        ConnectorHealthChanged,
        ConnectorSyncFailed,
        EcosystemKillSwitchChanged,
        InstallationChanged,
    )
    from app.services import notifications_service as ns

    async def admins(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        return await _holders(db, "integration:manage", "ecosystem:kill_switch")

    async def accountable(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        out = await _holders(db, "integration:manage")
        inst = _uuid(payload.get("installation_id"))
        if inst is not None:
            row = (
                await db.execute(
                    text("SELECT accountable_user_id FROM app_installations WHERE id = :i"), {"i": inst}
                )
            ).first()
            if row is not None and row[0] is not None:
                out.add(row[0])
        return out

    def _content(title: str, payload: Any, body: str | None = None) -> Any:
        return ns.Content(
            title=title, body=body, entity_type="installation", entity_id=_uuid(payload.get("installation_id")),
            link_path="/settings/integrations",
        )  # fmt: skip

    async def render_kill(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        state = "suspended" if payload.get("on") else "restored"
        return ns.Content(
            title=f"External access was {state} ({payload.get('scope')} switch)",
            link_path="/settings/integrations",
        )

    async def render_changed(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return _content(
            f"An integration was {str(payload.get('change', 'changed')).replace('_', ' ')}", payload
        )

    async def render_reconsent(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return _content(
            "An integration needs your consent again", payload, "Its new version asks for more access."
        )

    async def render_failing(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return _content(
            f"An integration sync keeps failing ({payload.get('error_code')})", payload,
            f"{payload.get('consecutive_failures')} consecutive failures of {payload.get('job_key')}.",
        )  # fmt: skip

    async def render_reconnect(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return _content(
            "An integration must be reconnected", payload, "The provider refused its credentials."
        )

    async def render_app_suspended(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return _content("An app was suspended by the platform", payload)

    defs = {
        "ecosystem_kill_switch_changed": ns.NotificationType(
            "Ecosystem kill switch", "Integrations", "both", True, admins, render_kill, internal_only=True
        ),
        "installation_changed": ns.NotificationType(
            "Integration changed", "Integrations", "in_app", False, admins, render_changed, internal_only=True
        ),
        "installation_reconsent_required": ns.NotificationType(
            "Integration needs consent",
            "Integrations",
            "both",
            False,
            admins,
            render_reconsent,
            internal_only=True,
        ),
        "connector_sync_failing": ns.NotificationType(
            "Integration sync failing",
            "Integrations",
            "both",
            False,
            accountable,
            render_failing,
            internal_only=True,
        ),
        "connector_reconnect_needed": ns.NotificationType(
            "Integration reconnect needed",
            "Integrations",
            "both",
            False,
            accountable,
            render_reconnect,
            internal_only=True,
        ),
        "app_suspended_by_platform": ns.NotificationType(
            "App suspended by the platform",
            "Integrations",
            "both",
            False,
            admins,
            render_app_suspended,
            internal_only=True,
        ),
    }
    fresh = [k for k in defs if k not in ns.NOTIFICATION_TYPES]
    for key in fresh:
        ns.register_notification_type(key, defs[key])
    if "ecosystem_kill_switch_changed" not in fresh:
        return
    ns.register_event_source(EcosystemKillSwitchChanged, "ecosystem_kill_switch_changed")

    def _installation(p: Any) -> str | None:
        change = p.get("change")
        if change == "reconsent_required":
            return "installation_reconsent_required"
        if change == "suspended" and p.get("actor_kind") == "platform":
            return "app_suspended_by_platform"
        return (
            "installation_changed" if change in ("installed", "uninstalled", "suspended", "resumed") else None
        )

    ns.register_event_source(InstallationChanged, _installation)
    ns.register_event_source(
        ConnectorSyncFailed,
        lambda p: "connector_sync_failing" if p.get("consecutive_failures") in (3, 10) else None,
    )
    ns.register_event_source(
        ConnectorHealthChanged,
        lambda p: "connector_reconnect_needed" if p.get("error_code") == "auth_failed" else None,
    )


_register()
