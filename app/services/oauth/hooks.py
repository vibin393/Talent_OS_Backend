# ruff: file-ignore[line-too-long]
"""OAuth registrations (Epic 66): the scope catalogue v1, the post-discovery scope validation (startup fails on a
non-delegable / ★ / unknown mapping) and the token notification types."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import oauth_scopes
from app.core.hooks import register_post_discovery
from app.services.oauth.scopes_v1 import register_v1

register_v1()


def _validate_scopes() -> None:
    problems = oauth_scopes.validate_catalogue()
    if problems:
        msg = "OAuth scope catalogue invalid: " + "; ".join(problems)
        raise RuntimeError(msg)


register_post_discovery(_validate_scopes)


def _register_notifications() -> None:
    from app.events.ecosystem import OAuthTokenFamilyRevoked
    from app.services import notifications_service as ns

    async def admins(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        from sqlalchemy import text

        rows = await db.execute(
            text(
                "SELECT DISTINCT ur.user_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
                "JOIN users u ON u.id = ur.user_id WHERE u.is_active AND (r.key = 'tenant_admin' "
                "OR EXISTS (SELECT 1 FROM jsonb_object_keys(r.permissions) k WHERE k = 'integration:manage'))"
            )
        )
        return {r[0] for r in rows}

    async def render(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(
            title="A connected app's refresh token was reused; its access was revoked",
            body="Someone may have copied the app's credentials. Review the installation before reconnecting.",
            entity_type="installation",
            entity_id=uuid.UUID(str(payload["installation_id"])) if payload.get("installation_id") else None,
        )

    if "oauth_refresh_reuse" not in ns.NOTIFICATION_TYPES:
        ns.register_notification_type(
            "oauth_refresh_reuse",
            ns.NotificationType(
                "Connected app token reuse", "Integrations", "both", True, admins, render, internal_only=True
            ),
        )
        ns.register_event_source(
            OAuthTokenFamilyRevoked,
            lambda p: "oauth_refresh_reuse" if p.get("reason") == "reuse_detected" else None,
        )


_register_notifications()

from app.services.oauth import hooks_e67  # ruff: ignore[unused-import]  (Epic 67 registrations)  # noqa: F401
