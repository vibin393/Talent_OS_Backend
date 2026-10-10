"""Sandbox Tenant creation (Epic 66, Story 66.9, FR-339, AD-85).

A Sandbox Tenant is provisioned through the normal path (``auth_service.create_tenant``: tenant row, default roles,
first-admin invitation, every registered provisioning step -- default Legal Entity, retention classes, ...), then
flipped to ``mode='sandbox'`` with its owner (``partner_org`` | ``tenant`` | ``platform``) and the ``sandbox`` rate tier,
and finally filled with the deterministic synthetic seed (``seed.py``). ``tenants.mode`` is written only here.
"""

import dataclasses
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Literal

from loguru import logger
from sqlalchemy import select, update

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import NotFound, ValidationFailed
from app.core.tenancy import clear_sandbox_cache
from app.models.identity import Tenant
from app.schemas.identity import TenantCreate, TenantOut
from app.services.sandbox import seed as seed_mod

OwnerKind = Literal["partner_org", "tenant", "platform"]
OWNER_KINDS: tuple[str, ...] = ("partner_org", "tenant", "platform")
DEFAULT_ADMIN_EMAIL = "sandbox-admin@example.com"  # RFC 2606 reserved; ``.test`` fails the EmailStr validator
SANDBOX_SEAT_LIMIT = 10
SANDBOX_RATE_TIER = "sandbox"


@dataclasses.dataclass(frozen=True, slots=True)
class SandboxCreated:
    tenant: TenantOut
    owner_kind: str
    owner_id: uuid.UUID | None
    seed: seed_mod.SeedResult


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "sandbox"
    return f"sbx-{base}-{secrets.token_hex(3)}"


async def _check_owner(owner_kind: str, owner_id: uuid.UUID | None, platform_user: uuid.UUID) -> uuid.UUID | None:
    if owner_kind not in OWNER_KINDS:
        raise ValidationFailed("Unknown sandbox owner kind", code="validation_failed", details={"fields": {"owner_kind": "invalid"}})
    if owner_kind == "platform":
        return owner_id or platform_user
    if owner_id is None:
        raise ValidationFailed(
            "owner_id is required for this owner kind", details={"fields": {"owner_id": "required"}}
        )
    async with platform_session() as db:
        if owner_kind == "tenant":
            row = (await db.execute(select(Tenant.mode).where(Tenant.id == owner_id))).scalar_one_or_none()
            if row is None:
                raise NotFound("Owner tenant not found")
            if row == "sandbox":
                raise ValidationFailed(
                    "A Sandbox Tenant cannot own another Sandbox Tenant", code="sandbox_owner_invalid"
                )
        else:
            from app.models.oauth import PartnerOrg

            if (await db.execute(select(PartnerOrg.id).where(PartnerOrg.id == owner_id))).scalar_one_or_none() is None:
                raise NotFound("Partner organisation not found")
    return owner_id


async def create(
    platform_ctx: RequestContext,
    *,
    name: str,
    owner_kind: OwnerKind,
    owner_id: uuid.UUID | None,
    size: seed_mod.Size,
    seed: int,
    admin_email: str | None = None,
) -> SandboxCreated:
    from app.services import auth_service

    principal = auth_service.require_platform(platform_ctx)
    if size not in seed_mod.SIZES:
        raise ValidationFailed("size must be small or medium", details={"fields": {"size": "invalid"}})
    owner = await _check_owner(owner_kind, owner_id, principal.id)

    data = TenantCreate(
        name=name,
        slug=_slug(name),
        seat_limit=SANDBOX_SEAT_LIMIT,
        admin_email=admin_email or DEFAULT_ADMIN_EMAIL,
    )
    tenant = await auth_service.create_tenant(platform_ctx, data)

    async with platform_session() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.id == tenant.id)
            .values(
                mode="sandbox",
                sandbox_owner_kind=owner_kind,
                sandbox_owner_id=owner,
                sandbox_last_active_at=datetime.now(UTC),
                rate_tier=SANDBOX_RATE_TIER,
            )
        )
        row = (await db.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        out = TenantOut.model_validate(row)
    clear_sandbox_cache(tenant.id)

    async with tenant_session(tenant.id) as tdb:
        result = await seed_mod.seed_tenant(tdb, tenant.id, size=size, seed=seed)
        audit.record(
            "tenant.sandbox_created",
            "tenant",
            tenant.id,
            session=tdb,
            owner_kind=owner_kind,
            owner_id=str(owner) if owner else None,
            size=size,
            seed=seed,
            counts=result.counts,
        )
    logger.info(
        "Sandbox Tenant created (tenant_id={}, owner_kind={}, size={}, seed={})", tenant.id, owner_kind, size, seed
    )
    return SandboxCreated(tenant=out, owner_kind=owner_kind, owner_id=owner, seed=result)
