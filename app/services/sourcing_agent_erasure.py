# ruff: noqa: E501
"""Sourcing Agent erasure handler ``sourcing_agent.shortlists`` (order 65), Epic 56 (FR-326, AD-34).

On Candidate erasure the subject's (and merged ids') ``agent_shortlist_entries`` (ranks, sources, explanation, drafts)
and ``agent_shortlist_dismissals`` rows are deleted. Shortlist headers hold counts only and stay. ``export`` lists the
Demand, rank, sources, status and dates. A top-level module so ``privacy`` never imports the Phase 4 domain.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.demands import Demand
from app.models.sourcing_agent import AgentShortlist, AgentShortlistDismissal, AgentShortlistEntry
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class SourcingShortlistsHandler:
    name = "sourcing_agent.shortlists"
    order = 65
    tables = ("agent_shortlist_entries", "agent_shortlist_dismissals")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        entries = await db.execute(
            delete(AgentShortlistEntry).where(AgentShortlistEntry.candidate_id.in_(ids))
        )
        dismissals = await db.execute(
            delete(AgentShortlistDismissal).where(AgentShortlistDismissal.candidate_id.in_(ids))
        )
        await db.flush()
        return {
            "entries": int(getattr(entries, "rowcount", 0) or 0),
            "dismissals": int(getattr(dismissals, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = (
            await db.execute(
                select(
                    Demand.req_id,
                    AgentShortlist.version,
                    AgentShortlistEntry.rank,
                    AgentShortlistEntry.sources,
                    AgentShortlistEntry.status,
                    AgentShortlistEntry.created_at,
                    AgentShortlistEntry.decided_at,
                    AgentShortlistEntry.outreach_sent_at,
                )
                .join(AgentShortlist, AgentShortlist.id == AgentShortlistEntry.shortlist_id)
                .join(Demand, Demand.id == AgentShortlistEntry.demand_id)
                .where(AgentShortlistEntry.candidate_id == ctx.candidate_id)
                .order_by(AgentShortlistEntry.created_at)
            )
        ).all()
        if not rows:
            return None
        return {
            "agent_shortlists": [
                {
                    "demand": r.req_id,
                    "version": r.version,
                    "rank": r.rank,
                    "sources": [s.get("kind") for s in (r.sources or []) if isinstance(s, dict)],
                    "status": r.status,
                    "listed_at": r.created_at,
                    "decided_at": r.decided_at,
                    "outreach_sent_at": r.outreach_sent_at,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(
                select(AgentShortlistEntry.id).where(AgentShortlistEntry.candidate_id.in_(ids)).limit(1)
            )
        ).first()
        left_d = (
            await db.execute(
                select(AgentShortlistDismissal.id)
                .where(AgentShortlistDismissal.candidate_id.in_(ids))
                .limit(1)
            )
        ).first()
        problems = []
        if left is not None:
            problems.append("agent shortlist entries remain")
        if left_d is not None:
            problems.append("agent shortlist dismissals remain")
        return problems


register_erasure_handler(SourcingShortlistsHandler())
