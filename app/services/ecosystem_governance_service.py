# ruff: file-ignore[line-too-long]
"""Epic 79 (Phase 5 Governance) facade: the one import surface for routers and tests.

Re-exports the ecosystem console read models, the data-flow register, violations, access reviews, erasure fan-out,
enablement checks, go-live checks, dashboards, the release-gate read model and sandbox purge.
"""

from app.services.ecosystem_governance import (
    access_reviews,
    console,
    dashboards,
    data_flows,
    enablement,
    erasure_fanout,
    golive_checks,
    release_gate,
    sandbox_purge,
    violations,
)

__all__ = [
    "access_reviews",
    "console",
    "dashboards",
    "data_flows",
    "enablement",
    "erasure_fanout",
    "golive_checks",
    "release_gate",
    "sandbox_purge",
    "violations",
]
