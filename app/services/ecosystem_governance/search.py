# ruff: file-ignore[line-too-long]
"""Phase 5 header search groups (Epic 79 story 79.4, spec A6). Registered from ``ecosystem_governance.hooks``.

| Group | Label | Permission | Source |
|---|---|---|---|
| ``installation`` | Installations | ``integration:view`` | name ILIKE over ``app_installations`` + API key names (prefix) |
| ``board_posting`` | Board postings | ``posting:publish_external`` | Epic 70 facade quick search (if present) |

``PENDING_SEARCH_GROUPS`` (shrink-only) lists groups whose owner function is missing; it must be empty at close.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import or_, select

from app.models.api_keys import ApiKey
from app.models.oauth import AppInstallation
from app.schemas.search import QuickHit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext

PENDING_SEARCH_GROUPS: dict[str, str] = {}
P5_SEARCH_GROUPS = {"installation": "Installations"}


async def search_installations(
    ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int  # ruff: ignore[unused-function-argument]
) -> list[QuickHit]:
    term = f"%{q.strip()}%"
    out: list[QuickHit] = []
    insts = (
        await db.execute(
            select(AppInstallation)
            .where(
                AppInstallation.status != "uninstalled",
                AppInstallation.display_name.ilike(term),
            )
            .limit(limit)
        )
    ).scalars().all()
    for inst in insts:
        out.append(
            QuickHit(
                id=inst.id,
                type="installation",
                title=inst.display_name[:200],
                subtitle=("App" if inst.kind == "app" else "Connector"),
                url=f"/admin/integrations?installation={inst.id}",
            )
        )
    keys = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.revoked_at.is_(None), or_(ApiKey.name.ilike(term), ApiKey.prefix.ilike(term)))
            .limit(limit)
        )
    ).scalars().all()
    for key in keys:
        out.append(
            QuickHit(
                id=key.id,
                type="installation",
                title=key.name[:200],
                subtitle=f"API key {key.prefix}…",
                url=f"/admin/integrations?api_key={key.id}",
            )
        )
    return out[:limit]


def register() -> None:
    from app.services.search import global_search as gs

    gs.register_global_group("installation", "Installations", "integration:view", search_installations)
    try:
        from app.services import job_distribution_service as jd

        fn = getattr(jd, "quick_search", None)
        if callable(fn):
            async def search_board_postings(ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int) -> list[QuickHit]:
                return await fn(ctx, db, q, kind, limit)

            gs.register_global_group(
                "board_posting", "Board postings", "posting:publish_external", search_board_postings
            )
            P5_SEARCH_GROUPS["board_posting"] = "Board postings"
        else:  # pragma: no cover
            PENDING_SEARCH_GROUPS["board_posting"] = "job board quick search missing"
    except Exception:  # pragma: no cover - owner module absent / renamed
        logger.debug("board_posting search group not registered (no owner quick search)")
        PENDING_SEARCH_GROUPS["board_posting"] = "job board quick search missing"


__all__ = ["P5_SEARCH_GROUPS", "PENDING_SEARCH_GROUPS", "register", "search_installations"]
