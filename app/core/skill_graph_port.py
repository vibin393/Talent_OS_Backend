# ruff: file-ignore[line-too-long]
"""Skills graph port for Phase 1 matching and search (Epic 37, AD-40 / AD-41).

P1 ``matching`` / ``search`` must not import the Phase 3 ``skills`` package (import-linter ``p3-p1p2-below``), and
``skills`` must not import them either. The skills domain registers its provider here from ``hooks.py``; P1 callers
use ``skill_graph()``. Without a provider (skills not loaded) matching and search keep their exact P1 behaviour.
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.skill_refs import ExpandedSkill, SkillKey

__all__ = ["SkillGraphPort", "register_skill_graph", "skill_graph"]


class SkillGraphPort(Protocol):
    async def graph_version(self, ctx: RequestContext, db: AsyncSession) -> str: ...

    async def expand(
        self, ctx: RequestContext, db: AsyncSession, refs: Sequence[SkillKey], *, depth: int, min_weight: Decimal
    ) -> list[ExpandedSkill]: ...

    async def explain_paths(
        self, ctx: RequestContext, db: AsyncSession, refs: Sequence[SkillKey], depth: int, *, min_weight: Decimal
    ) -> list[Any]: ...  # ``schemas.skills.ExpandedSkillOut`` rows (key, name, root, weight, depth, path)

    async def matching_settings(self, db: AsyncSession) -> Any: ...  # ``match_depth``, ``min_weight``

    async def keys_for_ids(self, db: AsyncSession, ids: list[uuid.UUID]) -> list[SkillKey]: ...


_provider: SkillGraphPort | None = None


def register_skill_graph(provider: SkillGraphPort | None) -> None:
    """Install (or, with ``None``, remove) the skills graph provider (Epic 37 ``skills/hooks.py``)."""
    global _provider  # process-wide wiring, set once from hooks
    _provider = provider


def skill_graph() -> SkillGraphPort | None:
    return _provider
