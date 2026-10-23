# ruff: file-ignore[line-too-long]
"""``/public/v1/submissions`` (Epic 67, spec A4): read only, scope ``submissions:read``. Generated shape; see ``services/public_api/delivery.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import SubmissionOut
from app.services import public_api_service as svc

router = public_router("submissions")
Read = scope("submissions:read")


@router.get("/submissions", response_model=Page[SubmissionOut])
async def list_submissions(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.delivery.list_submissions(db, **page.kw())


@router.get("/submissions/{submission_id}", response_model=SubmissionOut)
async def get_submission(submission_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.delivery.get_submission(db, submission_id)
