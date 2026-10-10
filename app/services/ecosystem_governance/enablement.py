# ruff: noqa: E501
"""Feature enablement checklist (Epic 79 story 79.7, FR-414, spec A9).

A registry ``register_enablement_check(feature, fn)`` lets owners register their own checks; Epic 79 provides defaults
for every externally-facing feature. Each check returns ``(key, label, status, detail)`` with status ``ok|warning|
blocking``. ``readiness(ctx, db)`` aggregates per feature; the feature status is the worst of its checks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.ecosystem_governance import EnablementCheckOut, FeatureReadinessOut

CheckFn = Callable[[RequestContext, AsyncSession], Awaitable[list[EnablementCheckOut]]]

FEATURES = (
    "job_network", "telephony", "stt", "ledger_sync", "hrms_sync", "job_boards",
    "calendar_sync", "mcp", "portal_copilot", "campaign_copy",
)  # fmt: skip

_REGISTRY: dict[str, list[CheckFn]] = {}

_WORST = {"ok": 0, "warning": 1, "blocking": 2}


def register_enablement_check(feature: str, fn: CheckFn) -> None:
    """Register an additional readiness check for ``feature`` (owners call this from their ``hooks.py``)."""
    _REGISTRY.setdefault(feature, []).append(fn)


def registered_features() -> list[str]:
    return sorted(set(FEATURES) | set(_REGISTRY))


# Default checks (spec A9 table). These are deliberately conservative: a feature with no owner check registered reports
# ``warning`` ("not verified here") rather than a false ``ok``, so the readiness page never over-promises.
_DEFAULTS: dict[str, tuple[tuple[str, str], ...]] = {
    "job_network": (("gstin", "Verified GSTIN and Legal Entity"), ("career_site", "Career site on"), ("terms", "Network terms accepted")),
    "telephony": (("recording_policy", "Recording Policy for each jurisdiction"), ("notices", "Screening / recording notices active"), ("caller_id", "A verified caller ID"), ("phone", "At least one recruiter with a verified calling phone"), ("cost_cap", "Cost cap set"), ("burn_in", "Provider Fake burn-in done")),
    "stt": (("install", "STT Installation active"), ("burn_in", "Fake burn-in done"), ("subprocessor", "Sub-processor entry present")),
    "ledger_sync": (("mappings", "Ledger mappings complete for each active Legal Entity"),),
    "hrms_sync": (("mappings", "HRMS field mappings complete"), ("notice", "HRMS transfer notice active")),
    "job_boards": (("accounts", "Board accounts configured per board"), ("notice", "Job board application notice active")),
    "calendar_sync": (("channel", "At least one connected calendar with a healthy push channel"),),
    "mcp": (("ai", "AI enablement on"), ("notice", "MCP notice accepted"), ("tool_groups", "Tool groups chosen")),
    "portal_copilot": (("ai", "AI enablement on"), ("notices", "Portal notices active"), ("redteam", "Red-team results green")),
    "campaign_copy": (("ai", "AI enablement on"), ("budget", "Budget set"), ("approver", "At least one approver")),
}


async def _default_checks(feature: str, ctx: RequestContext, db: AsyncSession) -> list[EnablementCheckOut]:  # noqa: ARG001
    return [
        EnablementCheckOut(key=key, label=label, status="warning", detail="Not verified in this environment yet.")
        for key, label in _DEFAULTS.get(feature, ())
    ]


async def feature_readiness(feature: str, ctx: RequestContext, db: AsyncSession) -> FeatureReadinessOut:
    checks = await _default_checks(feature, ctx, db)
    for fn in _REGISTRY.get(feature, []):
        try:
            checks.extend(await fn(ctx, db))
        except Exception:  # pragma: no cover - a broken owner check never breaks the page
            checks.append(EnablementCheckOut(key="error", label=feature, status="warning", detail="Check failed to run."))
    status = "ok"
    for c in checks:
        if _WORST[c.status] > _WORST[status]:
            status = c.status
    return FeatureReadinessOut(feature=feature, status=status, checks=checks)


async def readiness(ctx: RequestContext, db: AsyncSession) -> list[FeatureReadinessOut]:
    return [await feature_readiness(f, ctx, db) for f in registered_features()]


__all__ = [
    "FEATURES",
    "feature_readiness",
    "readiness",
    "register_enablement_check",
    "registered_features",
]
