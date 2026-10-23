# ruff: file-ignore[line-too-long]
"""``/public/v1/applications`` (Epic 67, spec A4): non-★ stage moves only (★ targets -> 403 ``human_only_action``)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Query, Response, status

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, Listing, etag, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import ApplicationCreateIn, ApplicationOut, StageMoveIn
from app.services import public_api_service as svc

router = public_router("applications")
Read = scope("applications:read")
Write = scope("applications:write")


@router.get("/applications", response_model=Page[ApplicationOut])
async def list_applications(
    _: Read, db: PublicDBSessionDep, page: Listing, demand_id: uuid.UUID | None = None, candidate_id: uuid.UUID | None = None,
    stage: Annotated[str | None, Query(max_length=60)] = None, updated_since: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    return await svc.applications.list_applications(db, **page.kw(), demand_id=demand_id, candidate_id=candidate_id, stage=stage, updated_since=updated_since)


@router.get("/applications/{application_id}", response_model=ApplicationOut)
async def get_application(application_id: uuid.UUID, _: Read, db: PublicDBSessionDep, response: Response) -> dict[str, Any]:
    out = await svc.applications.get_application(db, application_id)
    response.headers["ETag"] = etag(out)
    return out


@router.post("/applications", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
async def create_application(body: ApplicationCreateIn, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.applications.create_application(db, body))


@router.post("/applications/{application_id}/stage", response_model=ApplicationOut)
async def move_stage(application_id: uuid.UUID, body: StageMoveIn, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.applications.move_stage(db, application_id, body))
