# ruff: file-ignore[line-too-long]
"""Webhooks registrations (Epic 68): the outbox sink, the ``webhooks:manage`` scope (app-only, D-24), the partitioned
delivery log (66 partition job, 30-day retention), notification types, the settings hub entry, audit actions and the
after-commit handlers (verification kick, ``InstallationChanged``, kill-switch lift)."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

AUDIT_ACTIONS = (
    ("webhook.subscription_created", "A webhook subscription was created"),
    ("webhook.subscription_updated", "A webhook subscription was changed"),
    ("webhook.subscription_deleted", "A webhook subscription was deleted"),
    ("webhook.subscription_enabled", "A webhook subscription was turned on"),
    ("webhook.subscription_disabled", "A webhook subscription was turned off"),
    ("webhook.subscription_verified", "A webhook endpoint was verified"),
    ("webhook.subscription_verification_failed", "A webhook endpoint could not be verified"),
    ("webhook.secret_rotated", "A webhook signing secret was rotated"),
    ("webhook.replayed", "Webhook deliveries were replayed"),
    ("webhook.test_sent", "A webhook test event was sent"),
    ("webhook.erasure_no_endpoint", "An erasure notice had no webhook endpoint for an app"),
)


def _register_sink_and_scope() -> None:
    from app.core import oauth_scopes
    from app.services import public_events_service
    from app.services.webhooks import sink

    if "webhooks" not in {s.name for s in public_events_service.sinks()}:
        public_events_service.register_sink("webhooks", sink.on_rows)
    if "webhooks:manage" not in oauth_scopes.scopes():
        oauth_scopes.register_scope(
            oauth_scopes.ScopeSpec(
                key="webhooks:manage",
                label="Manage its own webhooks",
                description="Lets the app create and manage webhook subscriptions for the events its other permissions allow.",
                purpose_text="Receive event notifications from your workspace at the app's own endpoints.",
                permissions=(),
                api_key_allowed=False,  # D-24: never available to API keys
                owner_epic="68",
            )
        )


def _register_partitions() -> None:
    from app.services import metering_service

    metering_service.register_partitioned_table("webhook_deliveries", retention_days=30, time_column="created_at")


async def _recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    rows = await db.execute(
        text(
            "SELECT DISTINCT ur.user_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id JOIN users u ON u.id = ur.user_id "
            "WHERE u.is_active AND (r.key = 'tenant_admin' OR EXISTS (SELECT 1 FROM jsonb_object_keys(r.permissions) k WHERE k = 'integration:manage'))"
        )
    )
    out.update(r[0] for r in rows)
    sid = payload.get("subscription_id")
    if sid:
        row = (
            await db.execute(
                text(
                    "SELECT s.notify_user_id, i.accountable_user_id FROM webhook_subscriptions s "
                    "LEFT JOIN app_installations i ON i.id = s.installation_id WHERE s.id = CAST(:s AS uuid)"
                ),
                {"s": str(sid)},
            )
        ).first()
        if row is not None:
            out.update(u for u in row if u is not None)
    return out


def _register_notifications() -> None:
    from app.events.webhooks import WebhookSubscriptionChanged
    from app.services import notifications_service as ns

    async def _sub(db: AsyncSession, payload: Any) -> tuple[str, str]:
        row = (
            await db.execute(text("SELECT description, url_host FROM webhook_subscriptions WHERE id = CAST(:s AS uuid)"), {"s": str(payload.get("subscription_id"))})
        ).first()
        if row is None:
            return "webhook", ""
        return (row[0] or row[1] or "webhook"), (row[1] or "")

    def _content(title: str, payload: Any, body: str | None) -> Any:
        return ns.Content(title=title, body=body, entity_type="webhook_subscription", entity_id=payload.get("subscription_id"),
                          link_path=f"/settings/integrations/webhooks/{payload.get('subscription_id')}")  # fmt: skip

    reasons = {
        "gone": "the endpoint returned 410 Gone", "failing": "deliveries failed for 72 hours",
        "egress_refused": "the endpoint address became a private network address", "scope_lost": "the app no longer has access to these events",
        "uninstalled": "the app was uninstalled", "manual": "someone turned it off",
    }  # fmt: skip

    async def render_verification(db: AsyncSession, payload: Any) -> Any:
        name, host = await _sub(db, payload)
        return _content("Webhook endpoint couldn't be verified", payload, f"{name} ({host}) did not answer the verification challenge.")

    async def render_failing(db: AsyncSession, payload: Any) -> Any:
        name, host = await _sub(db, payload)
        return _content("Webhook deliveries are failing", payload, f"Deliveries to {name} ({host}) have been failing for over an hour.")

    async def render_disabled(db: AsyncSession, payload: Any) -> Any:
        name, host = await _sub(db, payload)
        why = reasons.get(str(payload.get("disabled_reason")), "of repeated failures")
        return _content("Webhook subscription turned off", payload, f"{name} ({host}) was turned off because {why}.")

    async def render_expiring(db: AsyncSession, payload: Any) -> Any:
        name, host = await _sub(db, payload)
        return _content("Old webhook signing secret expires soon", payload, f"The previous signing secret of {name} ({host}) stops working in 2 hours.")

    defs = {
        "webhook_verification_failed": ns.NotificationType("Webhook verification failed", "Integrations", "both", False, _recipients, render_verification, internal_only=True),
        "webhook_failing": ns.NotificationType("Webhook deliveries failing", "Integrations", "both", False, _recipients, render_failing, internal_only=True),
        "webhook_disabled": ns.NotificationType("Webhook turned off", "Integrations", "both", False, _recipients, render_disabled, internal_only=True),
        "webhook_secret_expiring": ns.NotificationType("Webhook secret expiring", "Integrations", "both", False, _recipients, render_expiring, internal_only=True),
    }
    fresh = [k for k in defs if k not in ns.NOTIFICATION_TYPES]
    for key in fresh:
        ns.register_notification_type(key, defs[key])
    if "webhook_failing" not in fresh:
        return

    def _choose(p: Any) -> str | None:
        change = p.get("change")
        if change == "verification_failed":
            return "webhook_verification_failed"
        if change == "failing":
            return "webhook_failing"
        if change == "secret_expiring":
            return "webhook_secret_expiring"
        if change == "disabled" and p.get("disabled_reason") in ("gone", "failing", "egress_refused", "scope_lost"):
            return "webhook_disabled"
        return None

    ns.register_event_source(WebhookSubscriptionChanged, _choose)


def _register_settings_and_audit() -> None:
    from app.core import audit
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "integrations", "webhooks", "Webhooks",
        "Send signed event notifications to your systems, see deliveries and replay failures",
        "/api/v1/webhooks", "webhook:manage", owner_epic="68",
    )  # fmt: skip
    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)


_SUBSCRIBED = {"done": False}


def _register_handlers() -> None:
    if _SUBSCRIBED["done"]:
        return
    _SUBSCRIBED["done"] = True
    from app.core.events import subscribe
    from app.events.ecosystem import EcosystemKillSwitchChanged, InstallationChanged
    from app.events.webhooks import WebhookSubscriptionChanged

    def on_subscription(e: WebhookSubscriptionChanged) -> None:
        if e.status == "pending_verification" and e.change in ("created", "updated", "enabled"):
            from app.tasks import webhooks as tasks
            from app.tasks.runtime import enqueue

            enqueue(tasks.verify, str(e.tenant_id), str(e.subscription_id))

    def on_installation(e: InstallationChanged) -> None:
        if e.kind == "app" and e.change in ("uninstalled", "granted", "upgraded"):
            from app.tasks import webhooks as tasks
            from app.tasks.runtime import enqueue

            enqueue(tasks.on_installation_changed, str(e.tenant_id), str(e.installation_id), e.change)

    def on_kill_switch(e: EcosystemKillSwitchChanged) -> None:
        if not e.on:
            from app.services.webhooks.common import kick_pump

            kick_pump(e.tenant_id)

    subscribe(WebhookSubscriptionChanged, on_subscription)
    subscribe(InstallationChanged, on_installation)
    subscribe(EcosystemKillSwitchChanged, on_kill_switch)


_register_sink_and_scope()
_register_partitions()
_register_notifications()
_register_settings_and_audit()
_register_handlers()
