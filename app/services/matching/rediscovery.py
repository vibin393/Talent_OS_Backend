# ruff: noqa: E501
"""Rediscovery summary registry (Epic 38, FR-194, decision 11; AD-40 registry pattern).

When a Demand opens, the Recruiter starts from the P1 match funnel plus *sections* registered by other domains:
Epic 38 registers ``pools`` (matching Talent Pools with their top members), Epic 39 adds its own. ``matching``
never imports those domains: they call ``register_rediscovery_section`` from their ``hooks.py``.

A loader is ``async (ctx, db, demand_id) -> dict | None`` (``None`` = section hidden for this viewer). A failing
loader is logged and omitted; it never breaks the summary.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError
from app.schemas.matching import RediscoveryOut

__all__ = ["SectionLoader", "rediscovery_summary", "register_rediscovery_section", "sections"]

SectionLoader = Callable[[RequestContext, AsyncSession, uuid.UUID], Awaitable[dict[str, Any] | None]]

_sections: dict[str, SectionLoader] = {}


def register_rediscovery_section(key: str, loader: SectionLoader) -> None:
    """Add (or replace) a section; keys are short domain names (``pools``, ``sequences``)."""
    if not key or not key.replace("_", "").isalnum():
        msg = f"bad rediscovery section key {key!r}"
        raise ValueError(msg)
    _sections[key] = loader


def sections() -> dict[str, SectionLoader]:
    return dict(_sections)


async def rediscovery_summary(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> RediscoveryOut:
    """``{funnel, sections}``; the funnel call enforces demand read (404 outside the caller's scope)."""
    from app.services.matching import service

    funnel = await service.get_funnel(ctx, db, demand_id)
    out: dict[str, Any] = {}
    for key, loader in sorted(_sections.items()):
        try:
            async with db.begin_nested():
                value = await loader(ctx, db, demand_id)
        except DomainError as exc:
            logger.info("rediscovery section {} skipped: {}", key, exc.code)
            continue
        except Exception:
            logger.exception("rediscovery section {} failed", key)
            continue
        if value is not None:
            out[key] = value
    return RediscoveryOut(demand_id=demand_id, funnel=funnel, sections=out)
