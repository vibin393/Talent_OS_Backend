# ruff: file-ignore[line-too-long]
"""Epic 79's own notification types (story 79.6, FR-411). Registered from ``ecosystem_governance.hooks``.

* ``access_review_opened`` -- a new quarterly access review is open (reviewers).
* ``access_review_auto_revoked`` -- a stale grant / authorisation / key was auto-revoked (accountable user).
* ``golive_check_failed`` -- a go-live / plaintext-free check failed (integration administrators; internal).

Owner notification types (installed / degraded / re-consent / webhook failing / ...) stay with their owners; the
coverage test ``tests/governance/test_p5_notification_coverage.py`` only verifies they are all registered.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _reviewers(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    from app.services.agent_runtime.hooks import _holders

    return await _holders(db, "integration:manage")


async def _accountable(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    uid = payload.get("user_id") if isinstance(payload, dict) else None
    if uid:
        try:
            return {uuid.UUID(str(uid))}
        except (ValueError, TypeError):
            pass
    from app.services.agent_runtime.hooks import _holders

    return await _holders(db, "integration:manage")


async def _integration_admins(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    from app.services.agent_runtime.hooks import _holders

    return await _holders(db, "integration:manage", "ecosystem:kill_switch")


def register() -> None:
    from app.services import notifications_service as ns

    def _render_opened(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(
            title="A new access review is open",
            body="Review the Installations, authorisations and API keys that have access to this tenant.",
            link_path="/admin/integrations/access-reviews",
        )

    def _render_auto(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(
            title="Access was removed automatically",
            body="A grant or key had not been used and was revoked during the access review.",
            link_path="/admin/integrations/access-reviews",
        )

    def _render_golive(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(
            title="A go-live check failed",
            body="An integrations readiness or plaintext-free check failed. Open the readiness page for details.",
            link_path="/settings/integrations",
        )

    specs = (
        ("access_review_opened", "A new access review is open", "Integrations", False, _reviewers, _render_opened, False),
        ("access_review_auto_revoked", "Access removed automatically", "Integrations", False, _accountable, _render_auto, False),
        ("golive_check_failed", "Go-live check failed", "Integrations", True, _integration_admins, _render_golive, True),
    )
    for key, label, group, mandatory, recipients, render, internal in specs:
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(
                key,
                ns.NotificationType(label, group, "in_app", mandatory, recipients, render, internal_only=internal),
            )


__all__ = ["register"]
