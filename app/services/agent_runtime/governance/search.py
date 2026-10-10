# ruff: noqa: E501, ARG001
"""Header search group ``agent_proposal`` (Epic 65 story 65.8, FR-329). Agent Proposals the caller can see: assigned
to them, owned by them, in a role queue they belong to, or every agent Proposal for ``ai:govern`` at tenant scope.
Copilot drafts are never returned. ``PRP-`` exact matches are pinned first. Titles only (no args / preview)."""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, Scope
from app.models.agent_runtime import AgentProposal
from app.schemas.search import QuickHit
from app.services.agent_runtime import proposals


async def _queue_keys(db: AsyncSession, principal: Principal) -> list[str]:
    if not principal.role_ids:
        return []
    keys = (
        await db.execute(
            select(AgentProposal.route_role_key).where(AgentProposal.route_role_key.is_not(None)).distinct()
        )
    ).scalars()
    return [k for k in keys if await proposals._role_member(db, principal, k)]


def visible_condition(principal: Principal, queue_keys: list[str]) -> Any:
    """``None`` = every agent Proposal of the tenant (``ai:govern`` at tenant scope)."""
    if principal.has("ai:govern", Scope.TENANT):
        return None
    return or_(
        AgentProposal.assignee_user_id == principal.id,
        AgentProposal.owner_user_id == principal.id,
        AgentProposal.route_role_key.in_(queue_keys or [""]),
    )


async def visible_filter(db: AsyncSession, principal: Principal) -> Any:
    return visible_condition(principal, await _queue_keys(db, principal))


async def quick_search_proposals(
    ctx: RequestContext, db: AsyncSession, q: str, classified: str, limit: int
) -> list[QuickHit]:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in ("user", "api_key", "app"):  # Epic 66: +app
        return []
    if not (principal.has("proposal:decide") or principal.has("ai:govern")):
        return []
    term = q.strip()
    stmt = select(AgentProposal).where(AgentProposal.origin == "agent")
    cond = await visible_filter(db, principal)
    if cond is not None:
        stmt = stmt.where(cond)
    exact = term.upper().startswith("PRP-")
    if exact:
        stmt = stmt.where(AgentProposal.human_id == term.upper())
    else:
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            or_(AgentProposal.title.ilike(like, escape="\\"), AgentProposal.human_id.ilike(like, escape="\\"))
        )
    rows = (await db.execute(stmt.order_by(AgentProposal.created_at.desc()).limit(limit))).scalars()
    return [
        QuickHit(
            id=p.id,
            type="agent_proposal",
            title=p.title[:200],
            subtitle=f"{p.agent_key} · {p.status}",
            human_id=p.human_id,
            url=f"/proposals?id={p.id}",
            exact=exact,
        )
        for p in rows
    ]


__all__ = ["quick_search_proposals", "visible_condition", "visible_filter"]
