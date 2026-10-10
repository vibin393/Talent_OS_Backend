"""Sandbox Tenants facade (Epic 66, Story 66.9, FR-339, AD-85).

* ``create_sandbox_tenant(platform_ctx, *, name, owner_kind, owner_id, size, seed) -> TenantOut``: provisions a tenant
  through the normal path, sets ``mode='sandbox'`` + owner + ``rate_tier='sandbox'``, then runs the deterministic seed.
* ``create_sandbox(platform_ctx, data) -> SandboxOut``: the same, with the owner and seed summary (platform endpoint).
* ``is_sandbox`` / ``clear_sandbox_cache``: re-exported from ``core.tenancy`` (60 s cache). Epic 75 (Job Network) and any
  cross-tenant analytics must skip tenants for which ``await is_sandbox(tenant_id)`` is True.
* Seed helpers: ``build_plan``, ``plan_digest``, ``content_digest``, ``is_reserved_email``, ``is_reserved_phone``.
"""

import uuid
from typing import Literal

from app.core.context import RequestContext
from app.core.tenancy import clear_sandbox_cache, is_sandbox
from app.schemas.identity import TenantOut
from app.schemas.sandbox import SandboxCreate, SandboxOut, SandboxSeedOut
from app.services.sandbox import tenants as _tenants
from app.services.sandbox.seed import (
    SIZES,
    SeedPlan,
    SeedResult,
    build_plan,
    content_digest,
    is_reserved_email,
    is_reserved_phone,
    plan_digest,
    read_content,
    seed_tenant,
    synthetic_contact_points,
)


async def create_sandbox_tenant(
    platform_ctx: RequestContext,
    *,
    name: str,
    owner_kind: Literal["partner_org", "tenant", "platform"],
    owner_id: uuid.UUID | None,
    size: Literal["small", "medium"],
    seed: int,
    admin_email: str | None = None,
) -> TenantOut:
    created = await _tenants.create(
        platform_ctx, name=name, owner_kind=owner_kind, owner_id=owner_id, size=size, seed=seed, admin_email=admin_email
    )
    return created.tenant


async def create_sandbox(platform_ctx: RequestContext, data: SandboxCreate) -> SandboxOut:
    created = await _tenants.create(
        platform_ctx,
        name=data.name,
        owner_kind=data.owner_kind,
        owner_id=data.owner_id,
        size=data.size,
        seed=data.seed,
        admin_email=data.admin_email,
    )
    return SandboxOut(
        tenant=created.tenant,
        mode="sandbox",
        owner_kind=created.owner_kind,  # type: ignore[arg-type]
        owner_id=created.owner_id,
        seed=SandboxSeedOut(
            size=created.seed.size,  # type: ignore[arg-type]
            seed=created.seed.seed,
            counts=created.seed.counts,
            plan_digest=created.seed.plan_digest,
        ),
    )


__all__ = [
    "SIZES",
    "SeedPlan",
    "SeedResult",
    "build_plan",
    "clear_sandbox_cache",
    "content_digest",
    "create_sandbox",
    "create_sandbox_tenant",
    "is_reserved_email",
    "is_reserved_phone",
    "is_sandbox",
    "plan_digest",
    "read_content",
    "seed_tenant",
    "synthetic_contact_points",
]
