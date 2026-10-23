# ruff: file-ignore[line-too-long]
"""``/public/v1/interviews`` (Epic 67, spec A4): read only, scope ``interviews:read``. Generated shape; see ``services/public_api/delivery.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import InterviewOut
from app.services import public_api_service as svc

router = public_router("interviews")
Read = scope("interviews:read")


@router.get("/interviews", response_model=Page[InterviewOut])
async def list_interviews(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.delivery.list_interviews(db, **page.kw())


@router.get("/interviews/{interview_id}", response_model=InterviewOut)
async def get_interview(interview_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.delivery.get_interview(db, interview_id)
