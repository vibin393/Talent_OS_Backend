# ruff: noqa: E501
"""Candidate Copilot (Epic 77, FR-396 / FR-399): ``/api/v1/portal/copilot`` (Candidate and Worker portals share it).
Candidate session (``CandidateDep``); the tenant must enable the candidate Copilot and the Candidate must hold the
``ai_agent_communication`` consent (``POST /consent``) before the first question. FR-80 guarded (worker)."""

from typing import Any

from fastapi import status

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.candidate import CandidateDep, require_candidate
from app.router.portal_copilot_routes import build
from app.schemas.portal_copilot import PortalConsentIn, PortalCopilotStatusOut
from app.services import portal_copilot_service as svc

router = build("candidate", "/portal/copilot", require_candidate, None, portal_guard_route("worker"))


@router.post("/consent", response_model=PortalCopilotStatusOut, status_code=status.HTTP_200_OK)
async def grant_consent(body: PortalConsentIn, principal: CandidateDep) -> Any:
    return await svc.grant_consent(get_context(), principal, body.notice_id)


__all__ = ["router"]
