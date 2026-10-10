# ruff: noqa: E501
"""``/public/v1/candidates`` (Epic 67, spec A4): ``candidates:read`` / ``candidates:write`` (+ ``pii.*`` unmask)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import File, Query, Request, Response, UploadFile, status

from app.core.errors import ValidationFailed
from app.core.public_api import public_router
from app.core.settings import get_settings
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, Listing, etag, guarded_update, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import (
    CandidateCreateIn,
    CandidateOut,
    CandidateUpdateIn,
    ResumeUploadOut,
)
from app.services import public_api_service as svc

router = public_router("candidates")
Read = scope("candidates:read")
Write = scope("candidates:write")


@router.get("/candidates", response_model=Page[CandidateOut])
async def list_candidates(
    _: Read, db: PublicDBSessionDep, page: Listing, updated_since: datetime | None = None, created_since: datetime | None = None,
    source: Annotated[str | None, Query(max_length=30)] = None, owner_id: uuid.UUID | None = None, tag: uuid.UUID | None = None,
) -> dict[str, Any]:  # fmt: skip
    return await svc.candidates.list_candidates(db, **page.kw(), updated_since=updated_since, created_since=created_since,
                                                source=source, owner_id=owner_id, tag=tag)  # fmt: skip


@router.get("/candidates/{candidate_id}", response_model=CandidateOut)
async def get_candidate(candidate_id: uuid.UUID, _: Read, db: PublicDBSessionDep, response: Response) -> dict[str, Any]:
    out = await svc.candidates.get_candidate(db, candidate_id)
    response.headers["ETag"] = etag(out)
    return out


@router.post("/candidates", response_model=CandidateOut, status_code=status.HTTP_201_CREATED)
async def create_candidate(body: CandidateCreateIn, _: Write, db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.candidates.create_candidate(db, body))


@router.patch("/candidates/{candidate_id}", response_model=CandidateOut)
async def update_candidate(candidate_id: uuid.UUID, body: CandidateUpdateIn, request: Request, _: Write, db: PublicDBSessionDep) -> dict[str, Any]:
    return await guarded_update(request, lambda: svc.candidates.current_version(db, candidate_id),
                                lambda: svc.candidates.update_candidate(db, candidate_id, body))  # fmt: skip


@router.post("/candidates/{candidate_id}/resume", response_model=ResumeUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_resume(candidate_id: uuid.UUID, _: Write, db: PublicDBSessionDep, idem: Idem, file: Annotated[UploadFile, File()]) -> Any:
    limit = get_settings().public_api_resume_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed("The resume is larger than 10 MB", code="file_too_large")
    return await idem.run(lambda: svc.candidates.upload_resume(db, candidate_id, data, file.filename))
