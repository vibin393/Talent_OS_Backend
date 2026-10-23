# ruff: file-ignore[line-too-long]
"""Worker portal "Onboarding help" (Epic 62 B7; ``/api/v1/portal/work/help``).

The signed-in Worker / pre-joining Candidate only (``WorkerDep``); another person's thread or message is a 404. The
service runs in an internal tenant session (it starts agent runs) and filters every read by the principal's
``candidate_id``; responses are the ``knowledge_portal`` projections (no run ids, outcomes or internal notes).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.database import tenant_session
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDep
from app.schemas.knowledge_portal import (
    PortalAskIn,
    PortalConsentIn,
    PortalFeedbackIn,
    PortalHelpState,
    PortalHelpThread,
)
from app.services import knowledge_service as svc

router = APIRouter(prefix="/portal/work/help", tags=["portal-work"], route_class=portal_guard_route("worker"))


async def _db(principal: WorkerDep) -> AsyncIterator[object]:
    async with tenant_session(principal.tenant_id) as session:
        yield session


DB = Annotated[object, Depends(_db)]


@router.get("", response_model=PortalHelpState)
async def state(principal: WorkerDep, db: DB) -> PortalHelpState:
    return await svc.qa.portal_state(db, principal.tenant_id, principal.candidate_id)  # type: ignore[arg-type]


@router.post("/consent", response_model=PortalHelpState)
async def consent(data: PortalConsentIn, principal: WorkerDep, db: DB) -> PortalHelpState:
    if data.decision == "accept":
        await svc.qa.grant_consent(db, principal.tenant_id, principal.candidate_id, data.notice_id)  # type: ignore[arg-type]
    return await svc.qa.portal_state(db, principal.tenant_id, principal.candidate_id)  # type: ignore[arg-type]


@router.get("/threads", response_model=PortalHelpState)
async def threads(principal: WorkerDep, db: DB) -> PortalHelpState:
    return await svc.qa.portal_state(db, principal.tenant_id, principal.candidate_id)  # type: ignore[arg-type]


@router.post("/threads", response_model=PortalHelpThread, status_code=201)
async def ask(data: PortalAskIn, principal: WorkerDep, db: DB) -> PortalHelpThread:
    return await svc.qa.ask(db, principal.tenant_id, principal.candidate_id, data.text)  # type: ignore[arg-type]


@router.get("/threads/{thread_id}", response_model=PortalHelpThread)
async def get_thread(thread_id: uuid.UUID, principal: WorkerDep, db: DB) -> PortalHelpThread:
    thread = await svc.qa.own_thread(db, principal.candidate_id, thread_id)  # type: ignore[arg-type]
    return await svc.qa.portal_thread(db, thread)  # type: ignore[arg-type]


@router.post("/threads/{thread_id}/messages", response_model=PortalHelpThread)
async def follow_up(
    thread_id: uuid.UUID, data: PortalAskIn, principal: WorkerDep, db: DB
) -> PortalHelpThread:
    return await svc.qa.ask(db, principal.tenant_id, principal.candidate_id, data.text, thread_id)  # type: ignore[arg-type]


@router.post("/threads/{thread_id}/handoff", response_model=PortalHelpThread)
async def handoff(thread_id: uuid.UUID, principal: WorkerDep, db: DB) -> PortalHelpThread:
    return await svc.qa.request_person(db, principal.candidate_id, thread_id)  # type: ignore[arg-type]


@router.post("/messages/{message_id}/feedback", response_model=PortalHelpThread)
async def feedback(
    message_id: uuid.UUID, data: PortalFeedbackIn, principal: WorkerDep, db: DB
) -> PortalHelpThread:
    return await svc.qa.feedback(db, principal.candidate_id, message_id, data.feedback, data.note)  # type: ignore[arg-type]


@router.post("/handoff", response_model=PortalHelpThread, status_code=201)
async def talk_to_coordinator(principal: WorkerDep, db: DB) -> PortalHelpThread:
    return await svc.qa.request_person(db, principal.candidate_id, None, principal.tenant_id)  # type: ignore[arg-type]
