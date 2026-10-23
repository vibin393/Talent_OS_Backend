# ruff: file-ignore[line-too-long]
"""Dashboard tile providers (Epic 79 story 79.3, FR-407). Read-only over owner facades and guarded reporting
projections; each provider degrades to an empty tile when its source is absent. ``available(ctx)`` gates a tile by
permission. Platform-table tiles (Job Network) come through ``job_network_service.tenant_stats`` (AD-87)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext

# tile key -> (title, permission, source facade note)
ECOSYSTEM_TILES: tuple[tuple[str, str, str], ...] = (
    ("api_requests", "API requests & error rate", "usage:read"),
    ("webhook_health", "Webhook success & latency", "integration:view"),
    ("connector_health", "Connector health by category", "integration:view"),
    ("job_board_performance", "Job-board performance", "integration:view"),
    ("sync_status", "HRMS & ledger sync status", "integration:view"),
    ("calendar_health", "Calendar sync health", "integration:view"),
    ("mcp_usage", "MCP usage & Proposal acceptance", "integration:view"),
    ("job_network", "Job Network performance", "integration:view"),
    ("telephony", "Telephony & STT minutes", "integration:view"),
    ("portal_copilot", "Portal Copilot usage", "integration:view"),
    ("campaign_copy", "Campaign copy approvals", "integration:view"),
)

PLATFORM_OUTCOME_TILES: tuple[tuple[str, str, str], ...] = (
    ("adoption", "Integration adoption (SM-40)", "report:read"),
    ("webhook_reliability", "Webhook reliability (SM-42)", "report:read"),
    ("install_success", "Install success (SM-44)", "report:read"),
    ("job_board_reach", "Job-board reach (SM-45)", "report:read"),
    ("back_office_sync", "Back-office sync (SM-46)", "report:read"),
    ("calendar_turnaround", "Calendar turnaround (SM-47)", "report:read"),
    ("mcp_safety", "MCP use & safety (SM-48)", "report:read"),
    ("integrity", "Integrity: ★ refusals, cross-tenant = 0 (SM-49)", "report:read"),
    ("network_value", "Network value (SM-50)", "report:read"),
    ("calls_with_transcripts", "Calls with transcripts (SM-51)", "report:read"),
    ("portal_copilot", "Portal Copilot (SM-52)", "report:read"),
    ("campaign_copy", "Campaign copy (SM-53)", "report:read"),
)


def tile_catalogue() -> dict[str, list[dict[str, str]]]:
    return {
        "ecosystem": [{"key": k, "title": t, "permission": p} for k, t, p in ECOSYSTEM_TILES],
        "platform_outcomes": [{"key": k, "title": t, "permission": p} for k, t, p in PLATFORM_OUTCOME_TILES],
    }


async def job_network_tile(ctx: RequestContext, db: AsyncSession) -> dict[str, Any]:
    """Job Network performance through the facade (AD-87: ``network_*`` readable only by ``job_network``)."""
    try:
        from app.services import job_network_service

        stats_fn = getattr(job_network_service, "tenant_stats", None)
        if stats_fn is None:
            return {}
        out = await stats_fn(ctx, db, None)
        return out.model_dump() if hasattr(out, "model_dump") else dict(out)
    except Exception:  # pragma: no cover - facade absent / no membership
        logger.debug("job_network tile unavailable")
        return {}


__all__ = ["ECOSYSTEM_TILES", "PLATFORM_OUTCOME_TILES", "job_network_tile", "tile_catalogue"]
