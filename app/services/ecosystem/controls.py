# ruff: file-ignore[line-too-long]
"""Ecosystem Kill Switch, platform suspensions, MCP enablement storage and the app policy (Epic 66, AD-86, FR-340/375).

* ``set_kill_switch`` (``ecosystem:kill_switch``): on -> App tokens and API keys on the Public API get 403
  ``ecosystem_suspended``, webhook deliveries pause (68), MCP sessions end (73), outbound connector jobs reschedule and
  inbound connector webhooks are held. **Lifting is ★** (``require_human``) and needs a reason.
* ``platform_set`` (platform operator): per tenant (``ecosystem_settings.platform_suspended_*``), per App
  (``apps.status = 'suspended'``) or global (``platform_ecosystem_controls``).
* ``set_mcp_enabled`` (``ai:agent_configure``, human only): storage for Epic 73's enablement UI.
* ``set_app_policy`` (``integration:manage``): never widens an existing Grant (it only gates future installs).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import NotFound, ValidationFailed
from app.core.events import emit
from app.core.tenancy import is_sandbox
from app.events.ecosystem import EcosystemKillSwitchChanged
from app.models.ecosystem import DEFAULT_APP_POLICY, PlatformEcosystemControls
from app.models.identity import Tenant
from app.models.oauth import App
from app.schemas.ecosystem import AppPolicyIn, EcosystemStateOut
from app.services.ecosystem.state import bust_cache, settings_row


def _now() -> datetime:
    return datetime.now(UTC)


def _actor_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


def _tenant(ctx: RequestContext) -> uuid.UUID:
    assert ctx.tenant_id is not None
    return ctx.tenant_id


async def state(ctx: RequestContext, db: AsyncSession) -> EcosystemStateOut:
    tenant_id = _tenant(ctx)
    row = await settings_row(db, tenant_id)
    async with platform_session() as pdb:
        glob = (
            await pdb.execute(select(PlatformEcosystemControls).where(PlatformEcosystemControls.id == 1))
        ).scalar_one()
        tenant = await pdb.get(Tenant, tenant_id)
    policy = {**DEFAULT_APP_POLICY, **(row.app_policy or {})}
    tier = row.rate_tier or (tenant.rate_tier if tenant is not None else "standard")
    sandbox = await is_sandbox(tenant_id)
    return EcosystemStateOut(
        kill_switch_on=row.kill_switch_at is not None,
        kill_switch_at=row.kill_switch_at,
        kill_switch_reason=row.kill_switch_reason,
        platform_suspended=row.platform_suspended_at is not None,
        platform_suspended_reason=row.platform_suspended_reason,
        global_suspended=glob.global_suspended_at is not None,
        mcp_enabled=row.mcp_enabled_at is not None,
        mcp_enabled_at=row.mcp_enabled_at,
        mcp_notice_version=row.mcp_notice_version,
        mcp_tool_groups=list(row.mcp_tool_groups or []),
        app_policy=AppPolicyIn.model_validate(policy),
        rate_tier="sandbox" if sandbox else tier,
        sandbox=sandbox,
    )


async def set_kill_switch(
    ctx: RequestContext, db: AsyncSession, *, on: bool, reason: str
) -> EcosystemStateOut:
    tenant_id = _tenant(ctx)
    require_human(
        actor_from_ctx(ctx)
    )  # FR-340: lifting is ★; switching on is human-only through the router as well
    if not reason or not reason.strip():
        raise ValidationFailed("A reason is required", details={"fields": {"reason": "Required"}})
    row = await settings_row(db, tenant_id, lock=True)
    if on:
        row.kill_switch_at, row.kill_switch_by, row.kill_switch_reason = (
            _now(),
            _actor_id(ctx),
            reason.strip()[:500],
        )
    else:
        row.kill_switch_at, row.kill_switch_by, row.kill_switch_reason = None, None, None
    audit.record(
        "ecosystem.kill_switch", "ecosystem_settings", row.id, session=db, on=on, reason=reason.strip()[:500]
    )
    emit(
        db,
        EcosystemKillSwitchChanged(
            tenant_id=tenant_id, scope="tenant", on=on, app_id=None, actor_id=_actor_id(ctx)
        ),
    )
    await db.flush()
    bust_cache(tenant_id)
    return await state(ctx, db)


async def platform_set(
    scope: str,
    *,
    tenant_id: uuid.UUID | None = None,
    app_id: uuid.UUID | None = None,
    on: bool,
    reason: str,
    operator_id: uuid.UUID,
) -> None:
    """Platform operator switch. Runs its own sessions (platform + the tenant's)."""
    reason = (reason or "").strip()[:500]
    if scope == "global":
        async with platform_session() as pdb:
            row = (
                await pdb.execute(
                    select(PlatformEcosystemControls)
                    .where(PlatformEcosystemControls.id == 1)
                    .with_for_update()
                )
            ).scalar_one()
            row.global_suspended_at = _now() if on else None
            row.global_suspended_by = operator_id if on else None
            row.global_suspended_reason = reason if on else None
            audit.record(
                "ecosystem.global_suspend", "platform_ecosystem_controls", None, session=pdb, on=on,
                actor_type="platform", actor_id=operator_id,
            )  # fmt: skip
        bust_cache()
        return
    if scope == "app":
        if app_id is None:
            raise ValidationFailed("app_id is required", details={"fields": {"app_id": "Required"}})
        async with platform_session() as pdb:
            app = (
                await pdb.execute(select(App).where(App.id == app_id).with_for_update())
            ).scalar_one_or_none()
            if app is None:
                raise NotFound("App not found")
            if on:
                app.status, app.suspended_reason = "suspended", reason
            elif app.status == "suspended":
                app.status, app.suspended_reason = "published", None
            audit.record(
                "ecosystem.app_suspend",
                "app",
                app.id,
                session=pdb,
                on=on,
                actor_type="platform",
                actor_id=operator_id,
            )
        bust_cache()
        return
    if scope != "tenant" or tenant_id is None:
        raise ValidationFailed("Unknown scope", details={"fields": {"scope": "tenant | app | global"}})
    async with tenant_session(tenant_id) as tdb:
        row = await settings_row(tdb, tenant_id, lock=True)
        row.platform_suspended_at = _now() if on else None
        row.platform_suspended_by = operator_id if on else None
        row.platform_suspended_reason = reason if on else None
        audit.record(
            "ecosystem.platform_suspend", "ecosystem_settings", row.id, session=tdb, on=on,
            actor_type="platform", actor_id=operator_id,
        )  # fmt: skip
        emit(
            tdb,
            EcosystemKillSwitchChanged(
                tenant_id=tenant_id, scope="platform_tenant", on=on, app_id=None, actor_id=operator_id
            ),
        )
    bust_cache(tenant_id)


async def set_mcp_enabled(
    ctx: RequestContext, db: AsyncSession, *, on: bool, notice_version: str | None, tool_groups: list[str]
) -> EcosystemStateOut:
    require_human(actor_from_ctx(ctx))  # FR-375
    tenant_id = _tenant(ctx)
    row = await settings_row(db, tenant_id, lock=True)
    if on:
        if not notice_version:
            raise ValidationFailed(
                "Acknowledge the MCP notice first",
                code="notice_required",
                details={"fields": {"notice_version": "Required"}},
            )
        row.mcp_enabled_at, row.mcp_enabled_by, row.mcp_notice_version = (
            _now(),
            _actor_id(ctx),
            notice_version,
        )
        row.mcp_tool_groups = sorted(set(tool_groups))
    else:
        row.mcp_enabled_at, row.mcp_enabled_by = None, None
    audit.record(
        "ecosystem.mcp_enabled",
        "ecosystem_settings",
        row.id,
        session=db,
        on=on,
        tool_groups=sorted(set(tool_groups)),
    )
    await db.flush()
    return await state(ctx, db)


async def set_app_policy(ctx: RequestContext, db: AsyncSession, policy: AppPolicyIn) -> EcosystemStateOut:
    require_human(actor_from_ctx(ctx))
    row = await settings_row(db, _tenant(ctx), lock=True)
    row.app_policy = policy.model_dump(mode="json")
    await db.flush()
    return await state(ctx, db)


async def set_tenant_rate_tier(tenant_id: uuid.UUID, rate_tier: str, *, operator_id: uuid.UUID) -> None:
    """Platform operator: ``tenants.rate_tier`` (Sandbox Tenants stay ``sandbox`` whatever is stored)."""
    if rate_tier not in ("sandbox", "standard", "enterprise"):
        raise ValidationFailed(
            "Unknown rate tier", details={"fields": {"rate_tier": "sandbox | standard | enterprise"}}
        )
    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id, with_for_update=True)
        if tenant is None:
            raise NotFound("Tenant not found")
        tenant.rate_tier = rate_tier
        audit.record(
            "ecosystem.rate_tier", "tenant", tenant_id, session=pdb, rate_tier=rate_tier,
            actor_type="platform", actor_id=operator_id,
        )  # fmt: skip


__all__ = [
    "platform_set",
    "set_app_policy",
    "set_kill_switch",
    "set_mcp_enabled",
    "set_tenant_rate_tier",
    "state",
]
