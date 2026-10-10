# ruff: noqa: E501
"""Epic 56 Sourcing Agent read tool ``pools.members`` (P3 talent pools, viewer data scope applied).

With ``pool_id``: that Pool's current members (keyset cursor = the last candidate id). With ``demand_id``: the
Demand's pinned Pools and the matching Pools of the rediscovery section (no member ids unless a ``pool_id`` is given).
"""

import uuid
from typing import Any

from pydantic import Field, model_validator

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import NotFound
from app.services.agent_tools.sourcing.common_e56 import OWNER_EPIC, VERSION, In, Out, scoped


class MembersIn(In):
    pool_id: uuid.UUID | None = None
    demand_id: uuid.UUID | None = None
    limit: int = Field(default=100, ge=1, le=200)
    cursor: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> "MembersIn":
        if self.pool_id is None and self.demand_id is None:
            raise ValueError("pool_id or demand_id is required")
        return self


class PoolRef(Out):
    pool_id: uuid.UUID
    name: str
    pinned: bool


class MembersOut(Out):
    pools: list[PoolRef]
    candidate_ids: list[uuid.UUID]
    next_cursor: uuid.UUID | None


async def _members(tctx: Any, args: MembersIn) -> ToolOutcome:
    from app.core.errors import DomainError
    from app.services import pools_service
    from app.services.talent_pools import rediscovery

    ctx = scoped(tctx)
    pools: list[PoolRef] = []
    if args.demand_id is not None:
        subject = tctx.subject
        if subject is not None and subject[0] == "demand" and subject[1] != args.demand_id:
            raise NotFound("Demand not found")
        pinned = {p.pool_id: p for p in await rediscovery.list_pins(ctx, tctx.db, args.demand_id)}
        pools.extend(PoolRef(pool_id=p.pool_id, name=p.name[:200], pinned=True) for p in pinned.values())
        section = await rediscovery.pools_section(ctx, tctx.db, args.demand_id) or {}
        for p in section.get("pools") or []:
            pid = uuid.UUID(str(p["pool_id"]))
            if pid not in pinned:
                pools.append(PoolRef(pool_id=pid, name=str(p.get("name") or "")[:200], pinned=False))
    ids: list[uuid.UUID] = []
    next_cursor = None
    if args.pool_id is not None:
        try:
            summary = await pools_service.pool_summary(ctx, tctx.db, args.pool_id)
        except DomainError as exc:
            raise NotFound("Pool not found") from exc
        if not any(p.pool_id == args.pool_id for p in pools):
            pools.append(PoolRef(pool_id=summary.id, name=summary.name[:200], pinned=False))
        ids = await pools_service.member_ids(
            ctx, tctx.db, args.pool_id, limit=args.limit + 1, after=args.cursor
        )
        if len(ids) > args.limit:
            ids = ids[: args.limit]
            next_cursor = ids[-1]
    return ToolOutcome(
        MembersOut(pools=pools[:60], candidate_ids=ids, next_cursor=next_cursor),
        refs=tuple(("talent_pool", p.pool_id) for p in pools[:60]),
        candidate_ids=tuple(ids),
        summary=f"{len(pools)} pools, {len(ids)} members",
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="pools.members", version=VERSION, owner_epic=OWNER_EPIC, title="Talent pool members",
        description="List the Demand's pinned and matching talent Pools (demand_id), and/or the current member candidate ids of one Pool you can see (pool_id; up to 200 per page, cursor = next_cursor). Read only.",
        input_model=MembersIn, output_model=MembersOut, permissions=("pool:read",), risk="read",
        scope=ScopeRule(resource="none", cross_subject=True), handler=_members,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
