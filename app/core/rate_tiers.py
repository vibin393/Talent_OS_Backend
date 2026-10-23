# ruff: file-ignore[line-too-long]
"""Rate Tiers (Epic 66, AD-78, FR-338). Pure: no I/O.

| Tier         | Per minute | Burst (per second) | Per day   | MCP calls/min/session | Delegated user/min |
|--------------|------------|--------------------|-----------|-----------------------|--------------------|
| ``sandbox``  | 60         | 10                 | 5,000     | 30                    | 60                 |
| ``standard`` | 600        | 50                 | 200,000   | 60                    | 120                |
| ``enterprise``| 3,000     | 200                | 2,000,000 | 120                   | 300                |

Values are overridable per field by the ``rate_tiers`` setting (``{"standard": {"per_minute": 900}}``). Effective
tier = ``app_installations.rate_tier`` ?? ``apps.rate_tier`` ?? ``ecosystem_settings.rate_tier`` ??
``tenants.rate_tier``; Sandbox Tenants are always ``sandbox`` (``resolve_tier_name``).
"""

import dataclasses
from collections.abc import Mapping

DEFAULT_TIER = "standard"


@dataclasses.dataclass(frozen=True, slots=True)
class RateTier:
    name: str
    per_minute: int
    burst_per_second: int
    per_day: int
    mcp_calls_per_min_per_session: int
    delegated_user_per_min: int


TIERS: Mapping[str, RateTier] = {
    "sandbox": RateTier("sandbox", 60, 10, 5_000, 30, 60),
    "standard": RateTier("standard", 600, 50, 200_000, 60, 120),
    "enterprise": RateTier("enterprise", 3_000, 200, 2_000_000, 120, 300),
}
_FIELDS = (
    "per_minute",
    "burst_per_second",
    "per_day",
    "mcp_calls_per_min_per_session",
    "delegated_user_per_min",
)


def tier(name: str | None, overrides: Mapping[str, Mapping[str, int]] | None = None) -> RateTier:
    """Return the tier ``name`` (unknown / empty -> ``standard``) with ``overrides`` (default: the ``rate_tiers`` setting)
    applied field by field. A name known only to ``overrides`` is built on top of ``standard``."""
    if overrides is None:
        from app.core.settings import get_settings

        overrides = get_settings().rate_tiers or {}
    key = (name or DEFAULT_TIER).strip().lower()
    base = TIERS.get(key)
    if base is None:
        if key not in overrides:
            key = DEFAULT_TIER
        base = dataclasses.replace(TIERS[DEFAULT_TIER], name=key)
    extra = {f: int(v) for f, v in (overrides.get(key) or {}).items() if f in _FIELDS and int(v) > 0}
    return dataclasses.replace(base, **extra) if extra else base


def resolve_tier_name(
    *,
    installation_tier: str | None,
    app_tier: str | None,
    ecosystem_tier: str | None,
    tenant_tier: str | None,
    sandbox: bool,
) -> str:
    """``installation ?? app ?? ecosystem_settings ?? tenant`` (blank values skipped); Sandbox Tenants: ``sandbox``."""
    if sandbox:
        return "sandbox"
    for value in (installation_tier, app_tier, ecosystem_tier, tenant_tier):
        if value and value.strip():
            return value.strip().lower()
    return DEFAULT_TIER


__all__ = ["DEFAULT_TIER", "TIERS", "RateTier", "resolve_tier_name", "tier"]
