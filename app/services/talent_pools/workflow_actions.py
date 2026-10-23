"""Workflow action ``talent_pools.add_to_pool`` (build plan section 5, spec A8).

Params ``{pool_id}``; static Pools only (a dynamic Pool is refused when the rule is saved, through the
``ActionDef.check`` hook, and again at run time); trigger entity types ``candidate`` / ``application``. Not a
human-only action (it performs no transition). Members are added with ``source='workflow'``.
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, ValidationFailed
from app.models.talent_pools import TalentPool

ACTION_KEY = "talent_pools.add_to_pool"
ENTITY_TYPES = ("candidate", "application")


class AddToPoolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_id: uuid.UUID


def _invalid(path: str, reason: str) -> ValidationFailed:
    return ValidationFailed(
        f"Invalid action at {path}: {reason}", code="invalid_action", details={"path": path, "reason": reason}
    )


def _validate(params: Any, entity_type: str, path: str) -> None:  # ruff: ignore[unused-function-argument]
    if entity_type not in ENTITY_TYPES:
        raise _invalid(path, "only for candidate or application triggers")


async def _check(db: AsyncSession, params: Any, path: str) -> None:
    pool = (await db.execute(select(TalentPool).where(TalentPool.id == params.pool_id))).scalar_one_or_none()
    if pool is None:
        raise _invalid(f"{path}.pool_id", "unknown_pool")
    if pool.kind != "static":
        raise _invalid(f"{path}.pool_id", "pool_is_dynamic")


def _candidate_id(ac: Any) -> uuid.UUID | None:
    if ac.entity_type == "candidate":
        return uuid.UUID(str(ac.entity_id))
    raw = (ac.snapshot or {}).get("candidate_id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def _plan(ac: Any) -> tuple[TalentPool, uuid.UUID]:
    from app.services.workflow.actions import ActionError

    candidate_id = _candidate_id(ac)
    if candidate_id is None:
        raise ActionError("candidate_unresolved", "The trigger has no candidate")
    pool = (
        await ac.db.execute(select(TalentPool).where(TalentPool.id == ac.params.pool_id))
    ).scalar_one_or_none()
    if pool is None:
        raise ActionError("pool_not_found", "The pool no longer exists")
    if pool.kind != "static":
        raise ActionError("pool_is_dynamic", "Members of a dynamic pool come from its segment")
    if pool.status != "active":
        raise ActionError("pool_archived", "The pool is archived")
    return pool, candidate_id


async def _run(ac: Any) -> str:
    from app.core.context import RequestContext
    from app.services.talent_pools import members
    from app.services.workflow.actions import ActionError

    pool, candidate_id = await _plan(ac)
    system = RequestContext(tenant_id=ac.tenant_id, actor_kind="workflow", actor_id=ac.rule_id)
    try:
        result = await members.add_members(
            system, ac.db, pool, [candidate_id], source="workflow", note=f"workflow rule {ac.rule_id}"
        )
    except DomainError as exc:
        raise ActionError(exc.code, exc.message) from exc
    return f"Added the candidate to pool {pool.name}" if result.added else f"Already in pool {pool.name}"


async def _dry(ac: Any) -> str:
    pool, _ = await _plan(ac)
    return f"Would add the candidate to pool {pool.name}"


def register() -> None:
    from app.services.workflow.actions import ACTIONS, ActionDef, register_action

    if ACTION_KEY in ACTIONS:
        return
    register_action(
        ACTION_KEY,
        ActionDef(
            "Add to talent pool",
            AddToPoolParams,
            _validate,
            _run,
            _dry,
            entity_types=ENTITY_TYPES,
            check=_check,
        ),
    )
