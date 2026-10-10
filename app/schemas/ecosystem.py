# ruff: noqa: E501
"""Ecosystem controls DTOs (Epic 66, spec 0.14 / 0.18)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PublisherKind = Literal["platform", "partner", "tenant"]


class AppPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publisher_kinds: list[PublisherKind] = Field(default_factory=lambda: ["platform", "partner"])
    private_apps: bool = True
    user_self_authorize: bool = True
    sensitive_approval_chain_id: uuid.UUID | None = None


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool
    reason: str = Field(min_length=1, max_length=500)


class McpEnableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool
    notice_version: str | None = Field(default=None, max_length=20)
    tool_groups: list[str] = Field(default_factory=list, max_length=40)


class PlatformSuspendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["tenant", "app", "global"]
    tenant_id: uuid.UUID | None = None
    app_id: uuid.UUID | None = None
    on: bool
    reason: str = Field(min_length=1, max_length=500)


class RateTierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate_tier: Literal["sandbox", "standard", "enterprise"]


class SuspendState(BaseModel):
    """``scope`` names the switch that suspends (``tenant`` | ``platform_tenant`` | ``platform_app`` | ``global``)."""

    suspended: bool
    scope: str | None = None
    reason: str | None = None


class EcosystemStateOut(BaseModel):
    kill_switch_on: bool
    kill_switch_at: datetime | None
    kill_switch_reason: str | None
    platform_suspended: bool
    platform_suspended_reason: str | None
    global_suspended: bool
    mcp_enabled: bool
    mcp_enabled_at: datetime | None
    mcp_notice_version: str | None
    mcp_tool_groups: list[str]
    app_policy: AppPolicyIn
    rate_tier: str
    sandbox: bool
