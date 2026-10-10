"""Ecosystem facade (Epic 66, spec 0.14; frozen signatures). Every Phase 5 epic checks suspension through
``is_suspended`` and never reads ``ecosystem_settings`` directly."""

from app.schemas.ecosystem import SuspendState
from app.services.ecosystem.controls import (
    platform_set,
    set_app_policy,
    set_kill_switch,
    set_mcp_enabled,
    set_tenant_rate_tier,
    state,
)
from app.services.ecosystem.state import bust_cache, is_suspended, settings_row

__all__ = [
    "SuspendState",
    "bust_cache",
    "is_suspended",
    "platform_set",
    "set_app_policy",
    "set_kill_switch",
    "set_mcp_enabled",
    "set_tenant_rate_tier",
    "settings_row",
    "state",
]
