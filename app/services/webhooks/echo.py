# ruff: noqa: E501, D401
"""The sandbox Echo endpoint (Epic 68, story 68.5, Known gap 4). Only Sandbox Tenants can create one (others: 404).

Token format ``<tenant_uuid_hex>.<random>`` lets the unauthenticated receiver open the right tenant session; only the
sha256 of the token is stored (constant-time match). Receipts keep ``TalentOS-*`` / ``Content-Type`` headers and the
body (<= 64 KB) for 7 days; ``signature_valid`` checks the matching Subscription's secrets; the verification challenge
is answered automatically."""

import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound
from app.core.signing import verify_v1
from app.core.tenancy import is_sandbox
from app.models.webhooks import WebhookEchoEndpoint, WebhookEchoReceipt, WebhookSubscription
from app.schemas.webhooks import EchoEndpointCreated, EchoEndpointOut, EchoReceiptOut
from app.services.webhooks.common import echo_prefix, user_id_of

MAX_PER_TENANT = 5
MAX_BODY = 65_536


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


async def _sandbox_only(ctx: RequestContext) -> None:
    from app.core.settings import get_settings

    if not get_settings().webhook_echo_enabled or not await is_sandbox(ctx.tenant_id):
        raise NotFound("Not found")


async def create_endpoint(ctx: RequestContext, db: AsyncSession, label: str | None) -> EchoEndpointCreated:
    await _sandbox_only(ctx)
    count = (await db.execute(select(func.count()).select_from(WebhookEchoEndpoint))).scalar_one()
    if count >= MAX_PER_TENANT:
        raise ConflictError("At most 5 echo endpoints per sandbox", code="echo_limit")
    assert ctx.tenant_id is not None
    token = f"{uuid.UUID(str(ctx.tenant_id)).hex}.{secrets.token_urlsafe(24)}"
    row = WebhookEchoEndpoint(id=uuid.uuid4(), tenant_id=ctx.tenant_id, token_hash=_hash(token), label=label, created_by_user_id=user_id_of(ctx))
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return EchoEndpointCreated(id=row.id, label=row.label, created_at=row.created_at, url=echo_prefix() + token)


async def list_endpoints(ctx: RequestContext, db: AsyncSession) -> list[EchoEndpointOut]:
    await _sandbox_only(ctx)
    rows = (await db.execute(select(WebhookEchoEndpoint).order_by(WebhookEchoEndpoint.created_at.desc()))).scalars()
    return [EchoEndpointOut(id=r.id, label=r.label, created_at=r.created_at) for r in rows]


async def list_receipts(ctx: RequestContext, db: AsyncSession, endpoint_id: uuid.UUID) -> list[EchoReceiptOut]:
    await _sandbox_only(ctx)
    ep = (await db.execute(select(WebhookEchoEndpoint).where(WebhookEchoEndpoint.id == endpoint_id))).scalar_one_or_none()
    if ep is None:
        raise NotFound("Echo endpoint not found")
    rows = (
        await db.execute(
            select(WebhookEchoReceipt).where(WebhookEchoReceipt.echo_token_hash == ep.token_hash)
            .order_by(WebhookEchoReceipt.received_at.desc()).limit(100)
        )
    ).scalars()  # fmt: skip
    return [
        EchoReceiptOut(
            id=r.id, received_at=r.received_at, event_type=(r.headers or {}).get("TalentOS-Event-Type"),
            signature_valid=r.signature_valid, size=len(r.body or b""), headers=dict(r.headers or {}),
            body=(r.body or b"").decode("utf-8", errors="replace"),
        )
        for r in rows
    ]


def _keep(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower().startswith("talentos-") or k.lower() == "content-type"}


def _header(headers: dict[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


async def receive(db: AsyncSession, tenant_id: uuid.UUID, token: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """Store one receipt in ``db`` (the tenant's session). Returns (status, response body)."""
    digest = _hash(token)
    eps = list((await db.execute(select(WebhookEchoEndpoint))).scalars())
    match = next((e for e in eps if hmac.compare_digest(bytes(e.token_hash), digest)), None)
    if match is None:
        return 404, b'{"error":"not_found"}'
    subs = list(
        (await db.execute(select(WebhookSubscription).where(WebhookSubscription.url.contains(token), WebhookSubscription.deleted_at.is_(None)))).scalars()
    )
    from app.services.webhooks import signing

    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    valid = False
    sub_id = None
    sig = _header(headers, "TalentOS-Signature") or ""
    for sub in subs:
        keys = await signing.signing_secrets(ctx, db, sub.id, purpose="webhook_echo_verify")
        if keys and verify_v1(sig, keys, body):
            valid, sub_id = True, sub.id
            break
    if sub_id is None and len(subs) == 1:
        sub_id = subs[0].id
    db.add(
        WebhookEchoReceipt(
            id=uuid.uuid4(), tenant_id=tenant_id, echo_token_hash=digest, subscription_id=sub_id,
            headers=_keep(headers), body=body[:MAX_BODY], signature_valid=valid,
        )
    )  # fmt: skip
    await db.flush()
    reply: dict[str, Any] = {"received": True}
    try:
        doc = json.loads(body)
        if isinstance(doc, dict) and doc.get("type") == "webhook.verification":
            reply = {"challenge": (doc.get("data") or {}).get("challenge")}
    except (ValueError, UnicodeDecodeError):
        pass
    return 200, json.dumps(reply).encode()


async def receive_public(token: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """The unauthenticated receiver: the token's tenant prefix selects the tenant session (unknown -> 404)."""
    head = token.split(".", 1)[0]
    try:
        tenant_id = uuid.UUID(hex=head)
    except ValueError:
        return 404, b'{"error":"not_found"}'
    if not await is_sandbox(tenant_id):
        return 404, b'{"error":"not_found"}'
    async with tenant_session(tenant_id) as db:
        return await receive(db, tenant_id, token, headers, body[:MAX_BODY])


__all__ = ["create_endpoint", "list_endpoints", "list_receipts", "receive", "receive_public"]
