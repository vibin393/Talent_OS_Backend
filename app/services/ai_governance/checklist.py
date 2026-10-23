# ruff: file-ignore[line-too-long]
"""Enablement checklist registry (Epic 65 story 65.3, FR-330).

``register_enablement_check(agent_key, key, fn, blocking=...)`` -- ``agent_key="*"`` applies to every agent. Each
check returns a ``CheckItem``; a check that raises shows as ``warning`` (owner facades are read best-effort, inside a
savepoint so a failing owner query never poisons the session). ``ready`` = no blocking item ``missing``.

The foundation's ``PUT /agents/{key}/enabled`` is not blocked server-side (frozen contract): the UI blocks enabling
until the blocking items are fixed, unless the user ticks "Enable anyway" (audited ``agent.checklist_overridden``).
"""

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Literal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import get_agent
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.schemas.ai_governance import CheckItemOut, ChecklistOut

Status = Literal["ready", "missing", "warning", "not_applicable"]


@dataclasses.dataclass(frozen=True, slots=True)
class CheckItem:
    key: str
    label: str
    status: Status
    detail: str
    fix_link: str | None = None


Check = Callable[[RequestContext, AsyncSession, str], Awaitable[CheckItem]]


@dataclasses.dataclass(frozen=True, slots=True)
class _Registered:
    agent_key: str
    key: str
    label: str
    fn: Check
    blocking: bool


_CHECKS: dict[tuple[str, str], _Registered] = {}


def register_enablement_check(
    agent_key: str, key: str, label: str, fn: Check, *, blocking: bool = False
) -> None:
    """Register a prerequisite of ``agent_key`` (``"*"`` = every agent). Re-registering replaces it."""
    _CHECKS[agent_key, key] = _Registered(agent_key, key, label, fn, blocking)


def checks_for(agent_key: str) -> list[_Registered]:
    general = [c for (a, _k), c in _CHECKS.items() if a == "*"]
    specific = [c for (a, _k), c in _CHECKS.items() if a == agent_key]
    return general + specific


async def evaluate(ctx: RequestContext, db: AsyncSession, agent_key: str) -> ChecklistOut:
    try:
        get_agent(agent_key)
    except KeyError as exc:
        raise NotFound("Agent not found") from exc
    from app.services.ai_governance import checks  # ruff: ignore[unused-import] - registers the checks

    items: list[CheckItemOut] = []
    ready = True
    for c in checks_for(agent_key):
        try:
            async with db.begin_nested():
                item = await c.fn(ctx, db, agent_key)
        except Exception as exc:
            logger.warning("Enablement check {}.{} failed: {}", agent_key, c.key, type(exc).__name__)
            item = CheckItem(c.key, c.label, "warning", "This prerequisite could not be checked right now.")
        if c.blocking and item.status == "missing":
            ready = False
        items.append(CheckItemOut(
            key=c.key, label=c.label, status=item.status, detail=item.detail, fix_link=item.fix_link, blocking=c.blocking,
        ))  # fmt: skip
    return ChecklistOut(agent_key=agent_key, ready=ready, items=items)


__all__ = ["Check", "CheckItem", "checks_for", "evaluate", "register_enablement_check"]
