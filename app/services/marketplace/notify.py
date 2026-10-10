# ruff: noqa: E501
"""Marketplace notification types (Epic 69, Part B): ``installation_pending_approval`` (approvers; in-app + email),
``installation_degraded`` (accountable user + ``integration:manage`` holders; in-app + email) and
``installation_test_failed`` (installer; in-app). Delivered directly in the caller's session."""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LINK = "/settings/integrations"


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


async def _approvers(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    rid = _uuid(payload.get("request_id"))
    if rid is None:
        return set()
    row = (
        await db.execute(
            text(
                "SELECT current_approver_user_id, current_approver_role_id FROM approval_requests WHERE id = :r"
            ),
            {"r": rid},
        )
    ).first()
    if row is None:
        return set()
    if row[0] is not None:
        return {row[0]}
    if row[1] is not None:
        users = await db.execute(
            text(
                "SELECT ur.user_id FROM user_roles ur JOIN users u ON u.id = ur.user_id WHERE ur.role_id = :r AND u.is_active"
            ),
            {"r": row[1]},
        )
        return {r[0] for r in users}
    return set()


async def _accountable(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
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


async def _installer(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
    user = _uuid(payload.get("user_id"))
    return {user} if user else set()


def _content(title: str, payload: Any, body: str | None = None) -> Any:
    from app.services import notifications_service as ns

    inst = _uuid(payload.get("installation_id"))
    return ns.Content(
        title=title, body=body, entity_type="installation", entity_id=inst,
        link_path=f"{LINK}/installed/{inst}" if inst else LINK,
    )  # fmt: skip


async def _render_pending(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    scopes = ", ".join(payload.get("sensitive_scopes") or [])
    return _content(
        f"Approve installing {payload.get('name') or 'an integration'}",
        payload,
        f"It asks for sensitive access: {scopes}." if scopes else None,
    )


async def _render_degraded(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    return _content(
        f"{payload.get('name') or 'An integration'} needs attention",
        payload,
        f"Its sync keeps failing ({payload.get('error_code')}).",
    )


async def _render_test_failed(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    return _content(
        f"We couldn't connect {payload.get('name') or 'an integration'}", payload, payload.get("message")
    )


def register() -> None:
    from app.services import notifications_service as ns

    defs = {
        "installation_pending_approval": ns.NotificationType(
            "Integration awaiting approval",
            "Integrations",
            "both",
            False,
            _approvers,
            _render_pending,
            internal_only=True,
        ),
        "installation_degraded": ns.NotificationType(
            "Integration degraded",
            "Integrations",
            "both",
            False,
            _accountable,
            _render_degraded,
            internal_only=True,
        ),
        "installation_test_failed": ns.NotificationType(
            "Integration test failed",
            "Integrations",
            "in_app",
            False,
            _installer,
            _render_test_failed,
            internal_only=True,
        ),
    }
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


async def send(db: AsyncSession, type_key: str, payload: dict[str, Any]) -> None:
    """Resolve, render and deliver one notification in ``db`` (never raises into the business flow)."""
    from app.services import notifications_service as ns

    definition = ns.NOTIFICATION_TYPES.get(type_key)
    if definition is None:
        return
    try:
        async with db.begin_nested():
            recipients = await definition.recipients_resolver(db, payload)
            if not recipients:
                return
            content = await definition.render(db, payload)
            await ns.deliver(
                db, type_key, recipients, content, event_id=f"{type_key}:{uuid.uuid4()}", payload=payload
            )
    except Exception:
        logger.exception("Marketplace notification {} failed", type_key)


__all__ = ["register", "send"]
