# ruff: noqa: E501
"""Release-gate read model (Epic 79 story 79.7, FR-405 / FR-414).

``latest_for_tenant`` returns the latest Conformance Kit / red-team / STT results for the platform Listings a Tenant
has installed (surfaced at ``GET /ecosystem-console/test-results``). The deterministic gate itself runs in CI via
``scripts/release_gate_p5.py``; scheduled live runs store results in the P4 ``ai_eval_runs`` tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext

SUITES = ("stt_wer", "mcp_redteam_live", "portal_redteam_live", "conformance")


async def latest_for_tenant(ctx: RequestContext, db: AsyncSession) -> list[dict[str, Any]]:
    """Latest results for the platform Listings installed by the Tenant. Degrades to an empty list."""
    try:
        from app.services import marketplace_service

        fn = getattr(marketplace_service, "latest_test_results", None)
        if callable(fn):
            return list(await fn(ctx, db))
    except Exception:  # pragma: no cover - owner absent / renamed
        logger.debug("release_gate.latest_for_tenant: no owner source")
    return []


__all__ = ["SUITES", "latest_for_tenant"]
