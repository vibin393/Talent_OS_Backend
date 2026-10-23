# ruff: file-ignore[line-too-long]
"""Demand Agent settings (Epic 55, A1): the per-tenant ``demand_agent_settings`` row, intake settings (mode, mailbox,
fallback owner, sender checks), verified sender domains and the agent thresholds / JD template / scan owner.

* Intake settings and sender domains: ``settings:manage``; agent settings: ``ai:agent_configure`` (GET also
  ``ai:govern``) -- enforced by the router.
* Public webmail domains are refused (422 ``public_domain_not_allowed``); an admin must confirm the mapping.
* ``rotate_address`` revokes the old platform token at once and creates a new one (audited).
"""

import re
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.settings import get_settings
from app.models.demand_intake import DemandAgentSettings, DemandIntakeRoute, DemandIntakeSenderDomain
from app.schemas.demand_intake import (
    AgentSettingsIn,
    AgentSettingsOut,
    IntakeSettingsIn,
    IntakeSettingsOut,
    SenderDomainIn,
    SenderDomainOut,
)
from app.services import entity_refs

PUBLIC_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
        "yahoo.co.in", "yahoo.in", "ymail.com", "rediffmail.com", "rediff.com", "icloud.com", "me.com", "mac.com",
        "aol.com", "proton.me", "protonmail.com", "zoho.com", "zohomail.in", "gmx.com", "mail.com", "yandex.com",
        "hey.com", "fastmail.com", "tutanota.com", "inbox.com",
    }
)  # fmt: skip
_DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def _now() -> datetime:
    return datetime.now(UTC)


def _principal_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


async def row(db: AsyncSession, *, create: bool = True) -> DemandAgentSettings:
    found = (await db.execute(select(DemandAgentSettings))).scalar_one_or_none()
    if found is not None:
        return found
    tenant_id = entity_refs.session_tenant(db)
    fresh = DemandAgentSettings(
        tenant_id=tenant_id, intake_mode="off", mailbox_folder="Talent OS Intake", accept_forwarded=True,
        require_sender_auth=True, roles_per_message_max=5, duplicate_threshold=Decimal("0.72"), duplicate_window_days=90,
        sla_risk_probability=Decimal("0.60"), sla_digest_max_items=25, ack_contact_on_intake=False,
    )  # fmt: skip
    if create:
        db.add(fresh)
        await db.flush()
        await db.refresh(fresh)
    return fresh


def platform_address(token: str | None) -> str | None:
    return f"req-{token}@{get_settings().demand_intake_email_domain}" if token else None


def normalise_domain(raw: str) -> str:
    domain = raw.strip().lower().lstrip("@").rstrip(".")
    if "@" in domain:
        domain = domain.rsplit("@", 1)[1]
    if not _DOMAIN_RE.fullmatch(domain):
        raise ValidationFailed("Enter a domain such as acme.com", code="domain_invalid")
    return domain


def is_public_domain(domain: str) -> bool:
    return domain.lower() in PUBLIC_DOMAINS


# ================================================================ intake settings
async def _mailbox_status(db: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    from app.services import connections_service
    from app.services.connections.provider import has_reply_scopes

    conn = await connections_service.get_active(db, user_id)
    if conn is None:
        return "not_connected"
    if not has_reply_scopes(conn.provider, conn.token_set.scopes, getattr(conn.impl, "name", None)):
        return "needs_reconnect"
    return "connected"


async def get_intake_settings(ctx: RequestContext, db: AsyncSession) -> IntakeSettingsOut:  # ruff: ignore[unused-function-argument]
    s = await row(db)
    warnings: list[str] = []
    status = await _mailbox_status(db, s.mailbox_user_id) if s.intake_mode == "connected_mailbox" else None
    if s.intake_mode == "connected_mailbox" and status != "connected":
        warnings.append("mailbox_needs_reconnect" if status == "needs_reconnect" else "mailbox_not_connected")
    if s.intake_mode != "off" and s.fallback_owner_id is None:
        warnings.append("fallback_owner_missing")
    return IntakeSettingsOut(
        intake_mode=s.intake_mode, mailbox_user_id=s.mailbox_user_id, mailbox_folder=s.mailbox_folder,  # type: ignore[arg-type]
        mailbox_status=status, fallback_owner_id=s.fallback_owner_id, accept_forwarded=s.accept_forwarded,  # type: ignore[arg-type]
        require_sender_auth=s.require_sender_auth,
        platform_address=platform_address(s.route_token) if s.intake_mode == "platform_address" else None,
        warnings=warnings,
    )  # fmt: skip


async def _active_internal_user(db: AsyncSession, user_id: uuid.UUID, field: str) -> None:
    if user_id not in await entity_refs.active_user_ids(db, [user_id]):
        raise ValidationFailed(
            "Choose an active user",
            code="invalid_user",
            details={"fields": [{"field": field, "message": "inactive or unknown user"}]},
        )


async def _new_route(db: AsyncSession, s: DemandAgentSettings) -> str:
    token = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(20))
    db.add(DemandIntakeRoute(token=token, tenant_id=s.tenant_id))
    s.route_token = token
    await db.flush()
    return token


async def _revoke_routes(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    rows = (
        await db.execute(
            select(DemandIntakeRoute).where(
                DemandIntakeRoute.tenant_id == tenant_id, DemandIntakeRoute.revoked_at.is_(None)
            )
        )
    ).scalars()
    for r in rows:
        r.revoked_at = _now()
    await db.flush()


async def update_intake_settings(
    ctx: RequestContext, db: AsyncSession, data: IntakeSettingsIn
) -> IntakeSettingsOut:
    s = await row(db)
    sent = data.model_fields_set
    for field in ("intake_mode", "mailbox_folder", "accept_forwarded", "require_sender_auth"):
        if field in sent and getattr(data, field) is not None:
            setattr(s, field, getattr(data, field))
    if "mailbox_user_id" in sent:
        s.mailbox_user_id = data.mailbox_user_id
    if "fallback_owner_id" in sent:
        if data.fallback_owner_id is not None:
            await _active_internal_user(db, data.fallback_owner_id, "fallback_owner_id")
        s.fallback_owner_id = data.fallback_owner_id
    if s.intake_mode != "off" and s.fallback_owner_id is None:
        raise ValidationFailed(
            "Choose a fallback owner", code="fallback_owner_required",
            details={"fields": [{"field": "fallback_owner_id", "message": "required unless intake is off"}]},
        )  # fmt: skip
    if s.intake_mode == "connected_mailbox":
        if s.mailbox_user_id is None:
            raise ValidationFailed("Choose the mailbox owner", code="mailbox_not_connected")
        status = await _mailbox_status(db, s.mailbox_user_id)
        if status == "not_connected":
            raise ValidationFailed("The mailbox owner has no connected mailbox", code="mailbox_not_connected")
        if status == "needs_reconnect":
            raise ValidationFailed(
                "The mailbox connection cannot read mail; reconnect it with mail reading enabled",
                code="mailbox_scopes_missing",
            )
        s.mailbox_status = "connected"
    if s.intake_mode == "platform_address" and not s.route_token:
        await _new_route(db, s)
    s.updated_by = _principal_id(ctx)
    await db.flush()
    return await get_intake_settings(ctx, db)


async def rotate_address(ctx: RequestContext, db: AsyncSession) -> IntakeSettingsOut:
    s = await row(db)
    await _revoke_routes(db, s.tenant_id)
    await _new_route(db, s)
    s.updated_by = _principal_id(ctx)
    audit.record("demand_intake.address_rotated", "demand_agent_settings", s.tenant_id, session=db)
    await db.flush()
    return await get_intake_settings(ctx, db)


# ================================================================ sender domains
async def _domain_out(db: AsyncSession, rows: list[DemandIntakeSenderDomain]) -> list[SenderDomainOut]:
    from app.services import clients_service

    users = await entity_refs.users(db, {r.verified_by for r in rows})
    out: list[SenderDomainOut] = []
    for r in rows:
        info = users.get(r.verified_by)
        out.append(
            SenderDomainOut(
                id=r.id,
                domain=str(r.domain),
                client_id=r.client_id,
                client_name=await clients_service.client_name(db, r.client_id),
                verified_by=r.verified_by,
                verified_by_name=info.name if info is not None else None,
                verified_at=r.verified_at,
                note=r.note,
            )
        )
    return out


async def list_sender_domains(ctx: RequestContext, db: AsyncSession) -> list[SenderDomainOut]:  # ruff: ignore[unused-function-argument]
    rows = list(
        (
            await db.execute(select(DemandIntakeSenderDomain).order_by(DemandIntakeSenderDomain.domain))
        ).scalars()
    )
    return await _domain_out(db, rows)


async def add_sender_domain(ctx: RequestContext, db: AsyncSession, data: SenderDomainIn) -> SenderDomainOut:
    from app.services import clients_service

    if not data.confirmed:
        raise ValidationFailed(
            "Confirm that this domain belongs to the client", code="confirmation_required",
            details={"fields": [{"field": "confirmed", "message": "required"}]},
        )  # fmt: skip
    domain = normalise_domain(data.domain)
    if is_public_domain(domain):
        raise ValidationFailed(
            "Public email domains (such as gmail.com) can't identify a client",
            code="public_domain_not_allowed",
        )
    if await clients_service.client_name(db, data.client_id) is None:
        raise ValidationFailed("Unknown client", code="invalid_reference")
    existing = (
        await db.execute(
            select(DemandIntakeSenderDomain).where(
                DemandIntakeSenderDomain.domain == domain,
                DemandIntakeSenderDomain.client_id == data.client_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return (await _domain_out(db, [existing]))[0]
    verifier = _principal_id(ctx)
    if verifier is None:
        raise NotFound("Resource not found")
    row_ = DemandIntakeSenderDomain(
        id=uuid.uuid4(), tenant_id=entity_refs.session_tenant(db), client_id=data.client_id, domain=domain,
        verified_by=verifier, verified_at=_now(), note=data.note,
    )  # fmt: skip
    db.add(row_)
    await db.flush()
    return (await _domain_out(db, [row_]))[0]


async def remove_sender_domain(ctx: RequestContext, db: AsyncSession, domain_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    found = await db.get(DemandIntakeSenderDomain, domain_id)
    if found is None:
        raise NotFound("Sender domain not found")
    await db.delete(found)
    await db.flush()


# ================================================================ agent settings
async def get_agent_settings(ctx: RequestContext, db: AsyncSession) -> AgentSettingsOut:  # ruff: ignore[unused-function-argument]
    s = await row(db)
    warnings: list[str] = []
    if s.sla_scan_owner_id is None:
        warnings.append("sla_scan_owner_missing")
    else:
        principal = await entity_refs.principal_for_user(db, s.tenant_id, s.sla_scan_owner_id)
        scope = principal.permissions.get("demand:read") if principal is not None else None
        if scope is None or str(getattr(scope, "value", scope)) != "tenant":
            warnings.append("sla_scan_owner_scope_below_tenant")
    return AgentSettingsOut(
        duplicate_threshold=s.duplicate_threshold, duplicate_window_days=s.duplicate_window_days,
        sla_risk_probability=s.sla_risk_probability, sla_digest_max_items=s.sla_digest_max_items,
        roles_per_message_max=s.roles_per_message_max, sla_scan_owner_id=s.sla_scan_owner_id,
        ack_contact_on_intake=s.ack_contact_on_intake, jd_template_md=s.jd_template_md, warnings=warnings,
    )  # fmt: skip


async def update_agent_settings(
    ctx: RequestContext, db: AsyncSession, data: AgentSettingsIn
) -> AgentSettingsOut:
    s = await row(db)
    sent = data.model_fields_set
    simple = (
        "duplicate_threshold", "duplicate_window_days", "sla_risk_probability", "sla_digest_max_items",
        "roles_per_message_max", "ack_contact_on_intake",
    )  # fmt: skip
    for field in simple:
        if field in sent and getattr(data, field) is not None:
            setattr(s, field, getattr(data, field))
    if "sla_scan_owner_id" in sent:
        if data.sla_scan_owner_id is not None:
            await _active_internal_user(db, data.sla_scan_owner_id, "sla_scan_owner_id")
        s.sla_scan_owner_id = data.sla_scan_owner_id
    if "jd_template_md" in sent:
        s.jd_template_md = (data.jd_template_md or "").strip() or None
    s.updated_by = _principal_id(ctx)
    await db.flush()
    return await get_agent_settings(ctx, db)


def snapshot(s: DemandAgentSettings) -> dict[str, Any]:
    return {
        "duplicate_threshold": s.duplicate_threshold,
        "duplicate_window_days": s.duplicate_window_days,
        "sla_risk_probability": s.sla_risk_probability,
        "sla_digest_max_items": s.sla_digest_max_items,
        "roles_per_message_max": s.roles_per_message_max,
    }


__all__ = [
    "PUBLIC_DOMAINS",
    "add_sender_domain",
    "get_agent_settings",
    "get_intake_settings",
    "is_public_domain",
    "list_sender_domains",
    "normalise_domain",
    "platform_address",
    "remove_sender_domain",
    "rotate_address",
    "row",
    "snapshot",
    "update_agent_settings",
    "update_intake_settings",
]
