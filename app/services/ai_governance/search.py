# ruff: file-ignore[line-too-long]
"""Phase 4 header search groups (Epic 65 story 65.8, FR-329). Registered from ``ai_governance.hooks``.

| Group | Label | Permission | Source |
|---|---|---|---|
| ``agent_proposal`` | Proposals | ``proposal:decide`` or ``ai:govern`` | ``agent_runtime.governance.search.quick_search_proposals`` |
| ``knowledge_source`` | Knowledge sources | ``knowledge:read`` | ``knowledge_service.quick_search`` (C-16), one hit per source |

``PENDING_SEARCH_GROUPS`` (shrink-only) lists groups whose owner function is missing; it must be empty at close.
"""

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.search import QuickHit

PENDING_SEARCH_GROUPS: dict[str, str] = {}
P4_SEARCH_GROUPS = {"agent_proposal": "Proposals", "knowledge_source": "Knowledge sources"}


async def search_proposals(
    ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int
) -> list[QuickHit]:
    from app.services.agent_runtime.governance.search import quick_search_proposals

    hits = await quick_search_proposals(ctx, db, q, kind, limit)
    hits.sort(key=lambda h: not h.exact)
    return hits[:limit]


async def search_knowledge(
    ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int
) -> list[QuickHit]:
    from app.services import knowledge_service

    out: dict[str, QuickHit] = {}
    for hit in await knowledge_service.quick_search(ctx, db, q, limit=limit):
        key = str(hit.source_id)
        if key not in out:
            out[key] = QuickHit(
                id=hit.source_id, type="knowledge_source", title=hit.title[:200], subtitle=f"Version {hit.source_version}",
                url=f"/settings/knowledge/{hit.source_id}",
            )  # fmt: skip
    return list(out.values())[:limit]


def register() -> None:
    from app.services.search import global_search as gs

    gs.register_global_group(
        "agent_proposal", "Proposals", ("proposal:decide", "ai:govern"), search_proposals
    )
    try:
        from app.services import knowledge_service

        if not callable(getattr(knowledge_service, "quick_search", None)):
            raise ImportError("knowledge_service.quick_search")
    except ImportError:
        PENDING_SEARCH_GROUPS["knowledge_source"] = "knowledge_service.quick_search missing"
        logger.warning("global search group knowledge_source not registered: owner function missing")
        return
    PENDING_SEARCH_GROUPS.pop("knowledge_source", None)
    gs.register_global_group("knowledge_source", "Knowledge sources", "knowledge:read", search_knowledge)


__all__ = ["P4_SEARCH_GROUPS", "PENDING_SEARCH_GROUPS", "register", "search_knowledge", "search_proposals"]
