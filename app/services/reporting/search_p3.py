# ruff: noqa: E501
"""Phase 3 global-search groups (Epic 52 A5, FR-262), registered from ``reporting.hooks``.

Each group delegates to the owner's ``quick_search(ctx, db, q, limit)`` (foundation 0.7), which applies the owner's
own scope / visibility rules. Reporting is the top-layer reader (AD-40) so it may import the Phase 3 facades;
``search`` itself imports none of them. A missing owner function -> the group is not registered and is listed in
``PENDING_SEARCH_GROUPS`` (must be empty at phase close).

Deviation (recorded in the Epic 52 notes): group keys equal the ``QuickHitType`` values (``talent_pool``,
``campaign``, ...), because ``global_search`` keeps only hits whose ``type`` equals the group key; the spec's
plural keys would drop every hit. ``report_definition`` uses ``report:read`` (the owner function returns only
definitions the viewer owns or that are shared with them).
"""

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.search import QuickHit

OwnerFn = Callable[[RequestContext, AsyncSession, str, int], Awaitable[list[Any]]]

# group key (= QuickHitType) -> (label, permission, owner module, owner function)
P3_SEARCH_GROUPS: dict[str, tuple[str, str, str, str]] = {
    "talent_pool": ("Talent Pools", "pool:read", "app.services.pools_service", "quick_search"),
    "campaign": ("Campaigns", "campaign:read", "app.services.campaigns_service", "quick_search"),
    "assessment": ("Assessments", "assessment:read", "app.services.assessments_service", "quick_search"),
    "campus_drive": ("Campus drives", "campus:read", "app.services.campus_service", "quick_search_drives"),
    "recruitment_event": ("Recruitment events", "campus:read", "app.services.campus_service", "quick_search_events"),
    "project": ("Projects", "project:read", "app.services.projects_service", "quick_search"),
    "immigration_case": ("Immigration cases", "immigration:read", "app.services.immigration_service", "quick_search"),
    "report_definition": ("Reports", "report:read", "app.services.reporting.builder", "quick_search_definitions"),
}  # fmt: skip

PENDING_SEARCH_GROUPS: dict[str, str] = {}


def _owner(module: str, name: str) -> OwnerFn | None:
    try:
        fn = getattr(importlib.import_module(module), name, None)
    except ImportError:
        return None
    return fn if callable(fn) else None


def _wrap(key: str, fn: OwnerFn) -> Callable[..., Awaitable[list[QuickHit]]]:
    async def search(ctx: RequestContext, db: AsyncSession, q: str, kind: str, limit: int) -> list[QuickHit]:  # noqa: ARG001
        hits: list[QuickHit] = []
        for hit in await fn(ctx, db, q, limit):
            h = hit if isinstance(hit, QuickHit) else QuickHit.model_validate(hit)
            if h.type != key:
                continue
            if h.human_id and h.human_id.upper() == q.strip().upper() and not h.exact:
                h = h.model_copy(update={"exact": True})  # e.g. "PRJ-000042" pinned first
            hits.append(h)
        hits.sort(key=lambda h: not h.exact)
        return hits[:limit]

    search.__qualname__ = f"search_{key}"
    return search


def register() -> None:
    from app.services.search import global_search as gs

    for key, (label, permission, module, name) in P3_SEARCH_GROUPS.items():
        fn = _owner(module, name)
        if fn is None:
            PENDING_SEARCH_GROUPS[key] = f"{module}.{name} missing"
            logger.warning("global search group {} not registered: owner function missing", key)
            continue
        PENDING_SEARCH_GROUPS.pop(key, None)
        gs.register_global_group(key, label, permission, _wrap(key, fn))
