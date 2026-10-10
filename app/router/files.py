"""Files: permission-checked download and generic attachments (``/api/v1``)."""

import uuid
from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from fastapi.responses import Response

from app.core.context import get_context
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.files import AttachmentOut
from app.services import files_service

router = APIRouter(tags=["files"])


def _content_disposition(filename: str) -> str:
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in filename) or "file"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get(
    "/files/{file_id}/download",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_file(file_id: uuid.UUID, _: CurrentUserDep, db: TenantDBSessionDep) -> Response:
    result = await files_service.download(get_context(), db, file_id)
    return Response(
        content=result.data,
        media_type=result.content_type,
        headers={
            "Content-Disposition": _content_disposition(result.filename),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{entity_type}/{entity_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    entity_type: str,
    entity_id: uuid.UUID,
    _: CurrentUserDep,
    db: TenantDBSessionDep,
    include_archived: Annotated[bool, Query()] = False,
) -> list[AttachmentOut]:
    return await files_service.list_attachments(
        get_context(), db, entity_type, entity_id, include_archived=include_archived
    )


@router.post(
    "/{entity_type}/{entity_id}/attachments",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_attachment(
    entity_type: str,
    entity_id: uuid.UUID,
    _: CurrentUserDep,
    db: TenantDBSessionDep,
    file: Annotated[UploadFile, File()],
    category: Annotated[str, Form()],
    expires_on: Annotated[date | None, Form()] = None,
    note: Annotated[str | None, Form(max_length=2000)] = None,
) -> AttachmentOut:
    data = await files_service.read_upload(file)
    return await files_service.add_attachment(
        get_context(),
        db,
        entity_type,
        entity_id,
        data=data,
        filename=file.filename,
        category=category,
        expires_on=expires_on,
        note=note,
    )


@router.delete("/attachments/{attachment_id}", response_model=AttachmentOut)
async def archive_attachment(
    attachment_id: uuid.UUID, _: CurrentUserDep, db: TenantDBSessionDep
) -> AttachmentOut:
    return await files_service.archive_attachment(get_context(), db, attachment_id)
