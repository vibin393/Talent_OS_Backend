# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Tenant / platform state the runtime reads (Epic 53): kill switches, features, agent config, action policies, limits,
monthly usage; and the concrete ``ToolContext``."""

import dataclasses
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.agentic.agents import AgentSpec
from app.core.agentic.copilot import enabled_capabilities
from app.core.agentic.types import RunLimits
from app.core.context import RequestContext
from app.core.permissions import Principal
from app.core.settings import get_settings
from app.models.agent_runtime import AgentActionPolicy, AgentConfig
from app.models.ai import AI_FEATURES, AIAction, AITenantSettings, PlatformAIControls
from app.services.ai import gateway


@dataclasses.dataclass(frozen=True, slots=True)
class Gates:
    global_kill: bool
    tenant_kill: bool
    platform_tenant_kill: bool
    features: Mapping[str, bool]
    settings: AITenantSettings | None
    provider_down_since: datetime | None

    @property
    def any_kill(self) -> bool:
        return self.global_kill or self.tenant_kill or self.platform_tenant_kill

    @property
    def agentic_on(self) -> bool:
        return bool(self.features.get("agentic", False))

    def agent_on(self, agent_key: str) -> bool:
        feature = f"agent_{agent_key}"
        if feature in AI_FEATURES:
            return bool(self.features.get(feature, False))
        return agent_key.startswith("_")  # test-only agents: master switch only


async def gates(db: AsyncSession) -> Gates:
    controls = (
        await db.execute(select(PlatformAIControls).where(PlatformAIControls.id == 1))
    ).scalar_one_or_none()
    row = (await db.execute(select(AITenantSettings))).scalar_one_or_none()
    return Gates(
        global_kill=get_settings().agents_globally_disabled
        or (controls is not None and controls.global_kill_switch_at is not None),
        tenant_kill=row is not None and row.kill_switch_at is not None,
        platform_tenant_kill=row is not None and row.platform_kill_switch_at is not None,
        features=gateway._features(row),
        settings=row,
        provider_down_since=controls.provider_down_since if controls is not None else None,
    )


async def config_for(db: AsyncSession, agent_key: str) -> AgentConfig | None:
    return (
        await db.execute(select(AgentConfig).where(AgentConfig.agent_key == agent_key))
    ).scalar_one_or_none()


async def action_policies(db: AsyncSession, agent_key: str) -> dict[str, AgentActionPolicy]:
    rows = await db.execute(select(AgentActionPolicy).where(AgentActionPolicy.agent_key == agent_key))
    return {r.tool_key: r for r in rows.scalars()}


def effective_limits(spec: AgentSpec, config: AgentConfig | None) -> RunLimits:
    if config is None or not config.limits:
        return spec.limits
    try:
        return spec.limits.narrowed({k: int(v) for k, v in config.limits.items()})
    except ValueError:
        return spec.limits


def allow_list(spec: AgentSpec, features: Mapping[str, bool], principal: Principal | None) -> frozenset[str]:
    """The agent's tools plus, for the Copilot, the tools of every capability that is on (C-4)."""
    keys = set(spec.tools)
    if spec.key == "copilot":
        for cap in enabled_capabilities(features, lambda p: principal is not None and principal.has(p)):
            keys.update(cap.tools)
    return frozenset(keys)


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def agent_tokens_this_month(db: AsyncSession, agent_key: str) -> int:
    tokens = func.coalesce(AIAction.input_tokens, 0) + func.coalesce(AIAction.output_tokens, 0)
    stmt = select(func.coalesce(func.sum(tokens), 0)).where(
        AIAction.agent_key == agent_key, AIAction.created_at >= month_start()
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


@dataclasses.dataclass
class ToolCtx:
    """Concrete ``core.agentic.types.ToolContext``."""

    request: RequestContext
    db: AsyncSession
    actor: ActorRef
    run_id: uuid.UUID | None
    agent_key: str
    owner_user_id: uuid.UUID
    subject: tuple[str, uuid.UUID] | None
    idempotency_key: str
    execution: Literal["agent", "approved_proposal", "copilot_confirm"]
    extras: dict[str, Any] = dataclasses.field(default_factory=dict)


__all__ = [
    "Gates",
    "ToolCtx",
    "action_policies",
    "agent_tokens_this_month",
    "allow_list",
    "config_for",
    "effective_limits",
    "gates",
    "month_start",
]
