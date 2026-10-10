# ruff: noqa: E501
"""Platform intake address (Epic 55, spec 0.3, ``platform_address`` mode).

``POST /api/v1/inbound/demand-intake`` receives raw RFC 822 from the inbound email provider:

* ``X-Intake-Signature: sha256=<hex HMAC of the body with settings.demand_intake_webhook_secret>`` -- checked
  first (401 ``bad_signature``; an empty secret refuses everything); rejections are audited without the payload;
* ``X-Intake-Recipient: req-<token>@<settings.demand_intake_email_domain>`` -- the token resolves to a tenant through
  the platform table ``demand_intake_routes`` (404 ``unknown_route`` for unknown / revoked tokens, after the
  signature check);
* <= 25 MB; at most 600 messages per token per hour (429).

The message is then ingested in that tenant's session only (cross-tenant isolation by construction). The Fake adapter
in tests is a direct POST signed with the test secret.
"""

import hashlib
import hmac
import re
import uuid

from loguru import logger
from sqlalchemy import select

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import NotFound, Unauthorized, ValidationFailed
from app.core.settings import get_settings
from app.models.demand_intake import DemandIntakeRoute
from app.schemas.demand_intake import IntakeReceiptOut
from app.services.demand_intake import receive

PER_TOKEN_LIMIT = 600
_RECIPIENT_RE = re.compile(
    r"^\s*(?:[^<]*<)?req-(?P<token>[a-z2-7]{8,24})@(?P<domain>[^>\s]+)>?\s*$", re.IGNORECASE
)


def signature_for(body: bytes, secret: str | None = None) -> str:
    key = (secret if secret is not None else get_settings().demand_intake_webhook_secret).encode("utf-8")
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def _valid_signature(raw: bytes, signature: str | None) -> bool:
    secret = get_settings().demand_intake_webhook_secret
    if not secret or not signature:
        return False
    given = signature.strip()
    if not given.startswith("sha256="):
        given = "sha256=" + given
    return hmac.compare_digest(given.lower(), signature_for(raw, secret))


async def _audit_rejection(reason: str) -> None:
    async with platform_session() as pdb:
        audit.record(
            "demand_intake.webhook_rejected", "demand_intake_route", None, session=pdb, reason=reason
        )


def token_of(recipient: str | None) -> str | None:
    if not recipient:
        return None
    m = _RECIPIENT_RE.match(recipient)
    if not m or m.group("domain").lower() != get_settings().demand_intake_email_domain.lower():
        return None
    return m.group("token").lower()


async def tenant_for_token(token: str) -> uuid.UUID | None:
    async with platform_session() as pdb:
        route = (
            await pdb.execute(select(DemandIntakeRoute).where(DemandIntakeRoute.token == token))
        ).scalar_one_or_none()
        if route is None or route.revoked_at is not None:
            return None
        return route.tenant_id


async def receive_platform_email(
    raw: bytes, recipient: str | None, signature: str | None
) -> IntakeReceiptOut:
    from app.core import rate_limit
    from app.services.demand_intake import settings as settings_mod

    if not _valid_signature(raw, signature):
        await _audit_rejection("bad_signature")
        raise Unauthorized("Invalid webhook signature", code="bad_signature")
    if len(raw) > receive.MAX_RAW_BYTES:
        await _audit_rejection("too_large")
        raise ValidationFailed("The email is larger than 25 MB", code="email_too_large")
    token = token_of(recipient)
    tenant_id = await tenant_for_token(token) if token else None
    if tenant_id is None:
        await _audit_rejection("unknown_route")
        raise NotFound("Unknown intake address", code="unknown_route")
    result = await rate_limit.enforce(
        "demand_intake_token", rate_limit.subject_hash(token or ""), PER_TOKEN_LIMIT, 3600
    )
    del result
    parsed = receive.parse_mime(raw)
    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    async with tenant_session(tenant_id) as db:
        settings = await settings_mod.row(db)
        if settings.intake_mode != "platform_address":
            logger.info("Demand intake webhook for a tenant whose intake mode is {}", settings.intake_mode)
        row, duplicate = await receive.ingest(
            ctx, db, settings, source="platform_address", parsed=parsed, raw=raw,
            external_ref=parsed.message_id, created_by_kind="system", created_by_id=None,
        )  # fmt: skip
        out = IntakeReceiptOut(message_id=row.id, status=row.status, duplicate=duplicate)
    return out


__all__ = ["receive_platform_email", "signature_for", "tenant_for_token", "token_of"]
