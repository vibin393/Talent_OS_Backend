# ruff: noqa: E501
"""Endpoint verification handshake (Epic 68, §0.3, FR-349). A signed ``webhook.verification`` envelope carries a
challenge; a 2xx within 10 s whose body is ``{"challenge": "<same>"}`` or the plain-text challenge activates the
Subscription. Automatic retries at 1 min, 10 min and 1 h, then a manual "Verify again"; the creator is notified
after the automatic retries fail (``webhook_verification_failed``)."""

import json
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.tenancy import is_sandbox
from app.models.webhooks import WebhookSubscription
from app.services.webhooks import common, delivery, signing
from app.services.webhooks.subscriptions import audit_sub, changed

RETRY_DELAYS = (60, 600, 3600)
EVENT_TYPE = "webhook.verification"


def body_for(tenant_id: uuid.UUID, slug: str, livemode: bool, subscription_id: uuid.UUID, challenge: str) -> tuple[uuid.UUID, bytes]:
    from app.services.public_events.outbox import iso_z

    event_id = uuid.uuid4()
    doc = {
        "id": str(event_id), "type": EVENT_TYPE, "version": 1, "api_version": "v1", "occurred_at": iso_z(common.now()),
        "sequence": 0, "tenant_ref": {"id": str(tenant_id), "slug": slug}, "livemode": bool(livemode),
        "subject": {"type": "webhook_subscription", "id": str(subscription_id)}, "data": {"challenge": challenge},
    }  # fmt: skip
    return event_id, signing.canonical(doc)


def challenge_echoed(content: bytes, challenge: str) -> bool:
    text = content.decode("utf-8", errors="replace").strip()
    if text == challenge:
        return True
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    return isinstance(doc, dict) and doc.get("challenge") == challenge


async def run(ctx: RequestContext, db: AsyncSession, sub: WebhookSubscription) -> bool:
    """One handshake attempt on ``sub`` (in ``db``). Returns True when the Subscription became active."""
    from app.services import public_events_service

    if sub.status != "pending_verification" or sub.deleted_at is not None:
        return sub.status == "active"
    slug, livemode = await public_events_service.tenant_ref(db, sub.tenant_id)
    challenge = secrets.token_urlsafe(24)
    event_id, body = body_for(sub.tenant_id, slug, livemode, sub.id, challenge)
    keys = await signing.signing_secrets(ctx, db, sub.id)
    if not keys:
        sub.verification_error = "no_secret"
        return False
    headers = signing.headers(keys, body, event_id=event_id, event_type=EVENT_TYPE, delivery_id=None, attempt=sub.verification_attempts + 1)
    result = await delivery.post(db, sub.tenant_id, sub.url, body, headers, sandbox=await is_sandbox(sub.tenant_id))
    sub.verification_attempts = (sub.verification_attempts or 0) + 1
    if result.ok and challenge_echoed(result.body, challenge):
        sub.status, sub.verified_at, sub.verification_error = "active", common.now(), None
        audit_sub(db, "subscription_verified", sub)
        changed(db, sub, "verified")
        return True
    if result.ok:
        sub.verification_error = "challenge_mismatch"
    elif result.error and result.error.startswith("egress_refused:"):
        sub.verification_error = result.error[:80]
    else:
        sub.verification_error = (result.error or "connection")[:80]
    final = sub.verification_attempts > len(RETRY_DELAYS)
    audit_sub(db, "subscription_verification_failed", sub, error=sub.verification_error, final=final)
    if final:
        changed(db, sub, "verification_failed")
    return False


def next_retry_delay(attempts: int) -> int | None:
    """Delay before the next automatic attempt after ``attempts`` attempts (None = stop, manual "Verify again")."""
    if 1 <= attempts <= len(RETRY_DELAYS):
        return RETRY_DELAYS[attempts - 1]
    return None


async def verify_now(ctx: RequestContext, db: AsyncSession, subscription_id: uuid.UUID) -> WebhookSubscription:
    sub = await common.load(ctx, db, subscription_id, lock=True)
    if sub.status == "pending_verification":
        await run(ctx, db, sub)
        await db.flush()
    return sub


async def verify_task(tenant_id: uuid.UUID, subscription_id: uuid.UUID) -> int | None:
    """Task body: run one attempt; return the delay of the next automatic retry (or None)."""
    from app.core.database import tenant_session

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        sub = (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == subscription_id).with_for_update())).scalar_one_or_none()
        if sub is None or sub.status != "pending_verification" or sub.deleted_at is not None:
            return None
        ok = await run(ctx, db, sub)
        return None if ok else next_retry_delay(sub.verification_attempts)


__all__ = ["EVENT_TYPE", "RETRY_DELAYS", "body_for", "challenge_echoed", "next_retry_delay", "run", "verify_now", "verify_task"]
