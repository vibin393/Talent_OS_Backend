# ruff: file-ignore[line-too-long]
"""AI agent Proposals (Epic 53, spec 0.16, FR-268/FR-269/FR-325). Deciding is ★ human only: API keys, workflow, agent
and system actors get 403 ``human_only_action``. Copilot drafts are confirmed / discarded by their own user only.
Epic 65 builds the inbox UI on these endpoints."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.pagination import Page
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.agentic import BulkDecisionIn, ConfirmDraftIn, ProposalDecisionIn, ProposalOut, ProposalQuery
from app.services import agent_runtime_service as svc

router = APIRouter(prefix="/proposals", tags=["ai-agents"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require_any("proposal:decide", "ai:govern"))]
Decide = Annotated[Principal, Depends(require("proposal:decide"))]
Idem = Annotated[IdempotencyGuard, Depends(idempotency())]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("", response_model=Page[ProposalOut])
async def list_proposals(_: Read, db: DB, q: Annotated[ProposalQuery, Query()]) -> Page[ProposalOut]:
    return await svc.list_proposals(get_context(), db, q)


@router.get("/{proposal_id}", response_model=ProposalOut)
async def get_proposal(proposal_id: uuid.UUID, _: CurrentUserDep, db: DB) -> ProposalOut:
    return await svc.get_proposal(get_context(), db, proposal_id)


@router.post("/bulk-decide", response_model=list[ProposalOut])
async def bulk_decide(body: BulkDecisionIn, _: Decide, db: DB, idem: Idem) -> Any:
    _human()
    return await idem.run(lambda: svc.bulk_decide(get_context(), db, body))


@router.post("/{proposal_id}/decide", response_model=ProposalOut)
async def decide(proposal_id: uuid.UUID, body: ProposalDecisionIn, _: Decide, db: DB, idem: Idem) -> Any:
    _human()
    return await idem.run(lambda: svc.decide_proposal(get_context(), db, proposal_id, body))


@router.post("/{proposal_id}/confirm", response_model=ProposalOut)
async def confirm(
    proposal_id: uuid.UUID, _: CurrentUserDep, db: DB, idem: Idem, body: ConfirmDraftIn | None = None
) -> Any:
    _human()
    return await idem.run(
        lambda: svc.confirm_draft(
            get_context(), db, proposal_id, edited_args=body.edited_args if body else None
        )
    )


@router.post("/{proposal_id}/discard", response_model=ProposalOut)
async def discard(proposal_id: uuid.UUID, _: CurrentUserDep, db: DB) -> ProposalOut:
    _human()
    return await svc.discard_draft(get_context(), db, proposal_id)
