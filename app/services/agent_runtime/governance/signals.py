# ruff: noqa: E501
"""Proposal review signals (Epic 65 story 65.2, SM-C14): how Proposals were reviewed, never what they said.

* ``mark_opened`` -- ``POST /ai-governance/proposals/{id}/opened`` (human only): upsert (proposal, user),
  ``first_opened_at`` kept, ``open_count += 1``. Only Proposals the caller can see (Copilot drafts excluded).
* ``record_decision`` -- from ``ProposalDecided`` (approved / edited_approved / rejected): the decider's row gets
  ``decided_at``, ``decision``, ``decision_ms`` and ``rubber_stamp`` (an approval never opened, or within 5 s).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import Principal
from app.models.agent_runtime import AgentProposal
from app.models.proposal_signals import REVIEW_DECISIONS, RUBBER_STAMP_MS, ProposalReviewSignal
from app.services.agent_runtime import proposals


def _now() -> datetime:
    return datetime.now(UTC)


async def mark_opened(ctx: RequestContext, db: AsyncSession, proposal_id: uuid.UUID) -> None:
    require_human(actor_from_ctx(ctx))
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise NotFound("Proposal not found")
    p = await db.get(AgentProposal, proposal_id)
    if p is None or p.origin != "agent" or not await proposals._visible(db, principal, p):
        raise NotFound("Proposal not found")
    now = _now()
    stmt = insert(ProposalReviewSignal).values(
        id=uuid.uuid4(), tenant_id=p.tenant_id, proposal_id=p.id, user_id=principal.id, first_opened_at=now,
        open_count=1,
    )  # fmt: skip
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenant_id", "proposal_id", "user_id"],
        set_={
            "first_opened_at": func.coalesce(
                ProposalReviewSignal.first_opened_at, stmt.excluded.first_opened_at
            ),
            "open_count": ProposalReviewSignal.open_count + 1,
            "updated_at": now,
        },
    )
    await db.execute(stmt)


async def record_decision(
    db: AsyncSession, proposal_id: uuid.UUID, decided_by: uuid.UUID | None, outcome: str
) -> ProposalReviewSignal | None:
    if outcome not in REVIEW_DECISIONS or decided_by is None:
        return None
    p = await db.get(AgentProposal, proposal_id)
    if p is None or p.origin != "agent":
        return None
    decided_at = p.decided_at or _now()
    row = (
        await db.execute(
            select(ProposalReviewSignal).where(
                ProposalReviewSignal.proposal_id == proposal_id, ProposalReviewSignal.user_id == decided_by
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ProposalReviewSignal(
            id=uuid.uuid4(), tenant_id=p.tenant_id, proposal_id=proposal_id, user_id=decided_by, open_count=0
        )
        db.add(row)
    row.decided_at = decided_at
    row.decision = outcome
    row.decision_ms = (
        max(0, int((decided_at - row.first_opened_at).total_seconds() * 1000))
        if row.first_opened_at
        else None
    )
    row.rubber_stamp = outcome in ("approved", "edited_approved") and (
        row.first_opened_at is None or (row.decision_ms is not None and row.decision_ms < RUBBER_STAMP_MS)
    )
    await db.flush()
    return row


__all__ = ["mark_opened", "record_decision"]
