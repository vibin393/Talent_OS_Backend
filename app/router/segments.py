# ruff: file-ignore[line-too-long]
"""Segments (Epic 38 story 38.1, FR-192): ``/api/v1/segments``.

Reading needs ``pool:read``; writing needs ``segment:write`` (definitions are validated against the author's field
permissions by ``core.semantic``). The builder catalogue is the foundation's ``GET /semantic/sources/{key}``.
Each endpoint calls one service function.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.segments import (
    SegmentCreate,
    SegmentOut,
    SegmentPreviewIn,
    SegmentPreviewOut,
    SegmentUpdate,
    SegmentVersionOut,
)
from app.services.segments import crud

router = APIRouter(prefix="/segments", tags=["segments"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("pool:read"))]
Write = Annotated[Principal, Depends(require("segment:write"))]


@router.get("", response_model=list[SegmentOut])
async def list_segments(
    _: Read, db: DB, include_archived: Annotated[bool, Query()] = False
) -> list[SegmentOut]:
    return await crud.list_segments(get_context(), db, include_archived=include_archived)


@router.post("/preview", response_model=SegmentPreviewOut)
async def preview(body: SegmentPreviewIn, _: Read, db: DB) -> SegmentPreviewOut:
    return await crud.preview(get_context(), db, body)


@router.post("", response_model=SegmentOut, status_code=status.HTTP_201_CREATED)
async def create_segment(body: SegmentCreate, _: Write, db: DB) -> SegmentOut:
    return crud.out(await crud.create_segment(get_context(), db, body))


@router.get("/{segment_id}", response_model=SegmentOut)
async def get_segment(segment_id: uuid.UUID, _: Read, db: DB) -> SegmentOut:
    return crud.out(await crud.get_visible(get_context(), db, segment_id))


@router.patch("/{segment_id}", response_model=SegmentOut)
async def update_segment(segment_id: uuid.UUID, body: SegmentUpdate, _: Write, db: DB) -> SegmentOut:
    return crud.out(await crud.update_segment(get_context(), db, segment_id, body))


@router.delete("/{segment_id}", response_model=SegmentOut)
async def archive_segment(segment_id: uuid.UUID, _: Write, db: DB) -> SegmentOut:
    return crud.out(await crud.archive_segment(get_context(), db, segment_id))


@router.get("/{segment_id}/versions", response_model=list[SegmentVersionOut])
async def segment_versions(segment_id: uuid.UUID, _: Read, db: DB) -> list[SegmentVersionOut]:
    return await crud.versions(get_context(), db, segment_id)
