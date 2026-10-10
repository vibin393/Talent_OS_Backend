# ruff: noqa: E501
"""Enablement, agent configuration, action policies and kill switches (Epic 53, stories 53.5 / 53.7, FR-268/271/272).

* ``acknowledge_enablement`` -- the tenant's master switch ``agentic`` goes on only with the current enablement-notice
  version (audited).
* ``set_agent_enabled`` -- toggles the AI feature ``agent_<key>``; the first enable starts a burn-in (no ``auto``).
* ``set_action_policy`` -- refuses ``auto`` for a ``high`` tool (422 ``autonomy_not_allowed``) and during burn-in.
* ``set_kill_switch`` / ``platform_set_kill_switch`` -- cancel queued runs, stop running / waiting runs, audit, notify.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.agentic.agents import AgentSpec, agent_triggers, agents, get_agent
from app.core.agentic.tools import get_tool
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import Principal, Scope
from app.core.settings import get_settings
from app.events.agents import KillSwitchChanged
from app.models.agent_runtime import AgentActionPolicy, AgentConfig, AgentRun
from app.models.ai import AITenantSettings, PlatformAIControls
from app.schemas.agentic import (
    ActionPolicyIn,
    AgentCatalogOut,
    AgentConfigIn,
    AgenticStateOut,
    AgentToolModeOut,
    AgentTriggerOut,
)
from app.services import directory_service
from app.services.agent_runtime import state
from app.services.agent_runtime.policy import PolicyInput, resolve
from app.services.agent_runtime.principals import intersect
from app.services.ai import gateway


def _now() -> datetime:
    return datetime.now(UTC)


def _actor_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


async def _settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> AITenantSettings:
    return await gateway._settings_row(db, tenant_id)


async def agentic_state(ctx: RequestContext, db: AsyncSession) -> AgenticStateOut:  # noqa: ARG001
    g = await state.gates(db)
    row = g.settings
    return AgenticStateOut(
        enabled=g.agentic_on,
        notice_version=row.agentic_notice_version if row is not None else None,
        current_notice_version=get_settings().agent_enablement_notice_version,
        enabled_at=row.agentic_enabled_at if row is not None else None,
        kill_switch=g.tenant_kill,
        kill_switch_at=row.kill_switch_at if row is not None else None,
        reason=row.kill_switch_reason if row is not None else None,
        platform_kill_switch=g.platform_tenant_kill,
        global_kill_switch=g.global_kill,
    )


async def acknowledge_enablement(
    ctx: RequestContext, db: AsyncSession, *, notice_version: str
) -> AgenticStateOut:
    if notice_version != get_settings().agent_enablement_notice_version:
        raise ConflictError(
            "The enablement notice changed; read the current version", code="notice_version_mismatch"
        )
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    row = await _settings_row(db, tenant_id)
    row.agentic_enabled_at = _now()
    row.agentic_enabled_by = _actor_id(ctx)
    row.agentic_notice_version = notice_version
    await gateway.set_features(db, tenant_id, {"agentic": True})
    audit.record("agentic.enabled", "ai_tenant_settings", row.id, session=db, notice_version=notice_version)
    await db.flush()
    return await agentic_state(ctx, db)


def _spec(agent_key: str) -> AgentSpec:
    try:
        return get_agent(agent_key)
    except KeyError as exc:
        raise NotFound("Agent not found") from exc


async def _config(db: AsyncSession, tenant_id: uuid.UUID, agent_key: str) -> AgentConfig:
    row = await state.config_for(db, agent_key)
    if row is None:
        row = AgentConfig(
            id=uuid.uuid4(), tenant_id=tenant_id, agent_key=agent_key, limits={}, triggers_enabled={}
        )
        db.add(row)
        await db.flush()
    return row


async def catalog_entry(
    ctx: RequestContext, db: AsyncSession, spec: AgentSpec, g: state.Gates | None = None
) -> AgentCatalogOut:
    g = g or await state.gates(db)
    config = await state.config_for(db, spec.key)
    policies = await state.action_policies(db, spec.key)
    principal = ctx.principal if isinstance(ctx.principal, Principal) else None
    allow = state.allow_list(spec, g.features, principal)
    permissions = intersect(principal.permissions, spec.permission_ceiling) if principal is not None else {}
    tools: list[AgentToolModeOut] = []
    for key in sorted(allow):
        try:
            tool = get_tool(key)
        except KeyError:
            continue
        policy = policies.get(key)
        decision = resolve(PolicyInput(
            global_kill=g.global_kill, tenant_kill=g.tenant_kill or g.platform_tenant_kill, agentic_on=g.agentic_on,
            agent_on=g.agent_on(spec.key), allow_list=allow, tool=tool, args=None,
            effective_permissions=permissions if principal is not None else dict.fromkeys(tool.permissions, Scope.TENANT),
            action_policy_mode=policy.mode if policy is not None else None,  # type: ignore[arg-type]
            config_default_mode=config.default_mode if config is not None else None,  # type: ignore[arg-type]
            spec_default_mode=spec.default_mode, origin=spec.origin,
            burn_in_until=config.burn_in_until if config is not None else None, now=_now(),
        ))  # fmt: skip
        tools.append(AgentToolModeOut(
            key=key, title=tool.title, risk=tool.risk, mode=decision.mode, mode_reason=decision.reason,
            route_role_key=policy.route_role_key if policy is not None else None, permissions=list(tool.permissions),
        ))  # fmt: skip
    triggers_enabled = (config.triggers_enabled if config is not None else None) or {}
    return AgentCatalogOut(
        key=spec.key, name=spec.name, purpose=spec.purpose, owner_epic=spec.owner_epic, version=spec.version,
        placeholder=spec.placeholder, enabled=g.agent_on(spec.key), model_tier=spec.model_tier,
        default_mode=(config.default_mode if config is not None and config.default_mode else spec.default_mode),
        burn_in_until=config.burn_in_until if config is not None else None,
        limits=state.effective_limits(spec, config).to_json(),
        monthly_token_budget=config.monthly_token_budget if config is not None else None,
        used_this_month=await state.agent_tokens_this_month(db, spec.key),
        permission_ceiling={k: str(v) for k, v in spec.permission_ceiling.items()},
        triggers=[AgentTriggerOut(key=t.key, kind=t.kind, enabled=bool(triggers_enabled.get(t.key, True))) for t in agent_triggers(spec.key)],
        tools=tools, eval_set=spec.eval_set, eval_threshold=spec.eval_threshold,
    )  # fmt: skip


async def get_agents(ctx: RequestContext, db: AsyncSession) -> list[AgentCatalogOut]:
    g = await state.gates(db)
    return [
        await catalog_entry(ctx, db, spec, g)
        for key, spec in sorted(agents().items())
        if not key.startswith("_")
    ]


async def set_agent_enabled(
    ctx: RequestContext, db: AsyncSession, agent_key: str, enabled: bool
) -> AgentCatalogOut:
    spec = _spec(agent_key)
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    g = await state.gates(db)
    if enabled and not g.agentic_on:
        raise ConflictError("Turn on AI agents for the workspace first", code="agentic_not_enabled")
    if enabled and spec.placeholder:
        raise ConflictError("This agent is not available yet", code="agent_not_available")
    config = await _config(db, tenant_id, agent_key)
    if enabled:
        config.enabled_at = _now()
        config.enabled_by = _actor_id(ctx)
        if config.burn_in_until is None and get_settings().agent_burn_in_days > 0:
            config.burn_in_until = _now() + timedelta(days=get_settings().agent_burn_in_days)
    await gateway.set_features(db, tenant_id, {f"agent_{agent_key}": enabled})
    audit.record(
        "agent.enabled" if enabled else "agent.disabled",
        "agent_config",
        config.id,
        session=db,
        agent_key=agent_key,
    )
    await db.flush()
    return await catalog_entry(ctx, db, spec)


def _burn_in(config: AgentConfig) -> bool:
    return config.burn_in_until is not None and _now() < config.burn_in_until


async def update_config(
    ctx: RequestContext, db: AsyncSession, agent_key: str, data: AgentConfigIn
) -> AgentCatalogOut:
    spec = _spec(agent_key)
    assert ctx.tenant_id is not None
    config = await _config(db, ctx.tenant_id, agent_key)
    fields = data.model_fields_set
    if data.limits is not None:
        try:
            spec.limits.narrowed(data.limits)
        except ValueError as exc:
            raise ValidationFailed(str(exc), code="limit_above_ceiling") from exc
        config.limits = dict(data.limits)
    if "default_mode" in fields:
        if data.default_mode == "auto" and _burn_in(config):
            raise ValidationFailed("Auto is not available during the burn-in period", code="burn_in_active")
        config.default_mode = data.default_mode
    if "monthly_token_budget" in fields:
        config.monthly_token_budget = data.monthly_token_budget
    if data.triggers_enabled is not None:
        known = {t.key for t in agent_triggers(agent_key)}
        unknown = sorted(set(data.triggers_enabled) - known)
        if unknown:
            raise ValidationFailed("Unknown trigger", code="unknown_trigger", details={"triggers": unknown})
        config.triggers_enabled = {**(config.triggers_enabled or {}), **data.triggers_enabled}
    if "channels" in fields:
        config.channels = list(data.channels) if data.channels is not None else None
    if "working_hours" in fields:
        config.working_hours = data.working_hours
    if "proposal_expiry_hours" in fields:
        config.proposal_expiry_hours = data.proposal_expiry_hours
    await db.flush()
    return await catalog_entry(ctx, db, spec)


async def set_action_policy(
    ctx: RequestContext, db: AsyncSession, agent_key: str, tool_key: str, data: ActionPolicyIn
) -> AgentCatalogOut:
    spec = _spec(agent_key)
    assert ctx.tenant_id is not None
    g = await state.gates(db)
    allow = state.allow_list(
        spec, g.features, ctx.principal if isinstance(ctx.principal, Principal) else None
    )
    if tool_key not in allow:
        raise ValidationFailed("This action is not on the agent's allow-list", code="tool_not_allowlisted")
    tool = get_tool(tool_key)
    config = await _config(db, ctx.tenant_id, agent_key)
    if data.mode == "auto" and tool.risk == "high":
        raise ValidationFailed("High-risk actions can never run automatically", code="autonomy_not_allowed")
    if data.mode == "auto" and _burn_in(config):
        raise ValidationFailed("Auto is not available during the burn-in period", code="burn_in_active")
    if (
        data.route_role_key is not None
        and await directory_service.role_id_by_key(db, data.route_role_key) is None
    ):
        raise ValidationFailed("Unknown role", code="unknown_role")
    row = (
        await db.execute(
            select(AgentActionPolicy).where(
                AgentActionPolicy.agent_key == agent_key, AgentActionPolicy.tool_key == tool_key
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgentActionPolicy(
            id=uuid.uuid4(), tenant_id=ctx.tenant_id, agent_key=agent_key, tool_key=tool_key,
            mode=data.mode or tool.default_mode or spec.default_mode,
        )  # fmt: skip
        db.add(row)
    elif data.mode is not None:
        row.mode = data.mode
    if "route_role_key" in data.model_fields_set:
        row.route_role_key = data.route_role_key
    row.updated_by = _actor_id(ctx)
    await db.flush()
    return await catalog_entry(ctx, db, spec)


# ================================================================ kill switches
async def stop_runs(db: AsyncSession) -> tuple[int, int]:
    """Cancel queued runs and stop running / waiting runs of the session's tenant (one statement each)."""
    now = _now()
    cancelled = await db.execute(
        update(AgentRun).where(AgentRun.status == "queued")
        .values(status="cancelled", reason_code="kill_switch", finished_at=now, lease_owner=None, lease_expires_at=None)
    )  # fmt: skip
    stopped = await db.execute(
        update(AgentRun).where(AgentRun.status.in_(("running", "waiting")))
        .values(status="stopped", reason_code="kill_switch", finished_at=now, lease_owner=None, lease_expires_at=None,
                wait_condition=None, wait_ref=None, wait_until=None)
    )  # fmt: skip
    return int(getattr(cancelled, "rowcount", 0) or 0), int(getattr(stopped, "rowcount", 0) or 0)


async def set_kill_switch(
    ctx: RequestContext, db: AsyncSession, *, on: bool, reason: str | None
) -> AgenticStateOut:
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    row = await _settings_row(db, tenant_id)
    if on:
        row.kill_switch_at, row.kill_switch_by, row.kill_switch_reason = (
            _now(),
            _actor_id(ctx),
            (reason or None) and reason[:500],
        )
        cancelled, stopped = await stop_runs(db)
    else:
        row.kill_switch_at, row.kill_switch_by, row.kill_switch_reason = None, None, None
        cancelled = stopped = 0
    audit.record(
        "agents.kill_switch",
        "ai_tenant_settings",
        row.id,
        session=db,
        on=on,
        cancelled=cancelled,
        stopped=stopped,
    )
    emit(db, KillSwitchChanged(tenant_id=tenant_id, scope="tenant", on=on, actor_id=_actor_id(ctx)))
    await db.flush()
    return await agentic_state(ctx, db)


async def platform_set_kill_switch(
    ctx: RequestContext, db: AsyncSession, *, tenant_id: uuid.UUID | None, on: bool, reason: str | None
) -> None:
    """Platform operator: per tenant (``platform_kill_switch_*``) or global (``platform_ai_controls``). ``db`` is the
    platform session; tenant work runs in its own tenant session."""
    actor = _actor_id(ctx)
    if tenant_id is None:
        controls = (
            await db.execute(select(PlatformAIControls).where(PlatformAIControls.id == 1))
        ).scalar_one()
        controls.global_kill_switch_at = _now() if on else None
        controls.global_kill_switch_by = actor if on else None
        controls.global_kill_switch_reason = ((reason or None) and reason[:500]) if on else None
        audit.record("agents.global_kill_switch", "platform_ai_controls", None, session=db, on=on)
        await db.flush()
        if on:
            from app.tasks import agents as agent_tasks
            from app.tasks.runtime import enqueue

            enqueue(agent_tasks.stop_all_tenants)
        return
    async with tenant_session(tenant_id) as tdb:
        row = await _settings_row(tdb, tenant_id)
        row.platform_kill_switch_at = _now() if on else None
        row.platform_kill_switch_by = actor if on else None
        row.platform_kill_switch_reason = ((reason or None) and reason[:500]) if on else None
        if on:
            await stop_runs(tdb)
        audit.record(
            "agents.platform_kill_switch",
            "ai_tenant_settings",
            row.id,
            session=tdb,
            on=on,
            actor_type="platform",
            actor_id=actor,
        )
        emit(tdb, KillSwitchChanged(tenant_id=tenant_id, scope="platform_tenant", on=on, actor_id=actor))


__all__ = [
    "acknowledge_enablement",
    "agentic_state",
    "catalog_entry",
    "get_agents",
    "platform_set_kill_switch",
    "set_action_policy",
    "set_agent_enabled",
    "set_kill_switch",
    "stop_runs",
    "update_config",
]
