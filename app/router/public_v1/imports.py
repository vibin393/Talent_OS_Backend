# ruff: noqa: E501
"""``/public/v1/imports`` (Epic 67, spec A4): asynchronous bulk import of candidates or applications (CSV / JSONL)."""

import uuid
from typing import Annotated, Any

from fastapi import File, Form, Response, UploadFile, status

from app.core.errors import ValidationFailed
from app.core.public_api import public_router
from app.core.settings import get_settings
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, scope
from app.schemas.public_v1.resources import ImportJobOut
from app.services import public_api_service as svc

router = public_router("imports")
Write = scope("imports:write")


@router.post("/imports", response_model=ImportJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_import(
    _: Write, db: PublicDBSessionDep, idem: Idem, file: Annotated[UploadFile, File()],
    resource: Annotated[str, Form(max_length=20)], format: Annotated[str, Form(max_length=10)],
    consent_basis: Annotated[str | None, Form(max_length=30)] = None,
) -> Any:  # fmt: skip
    limit = get_settings().public_api_bulk_max_file_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed("The file is too large", code="file_too_large")
    return await idem.run(lambda: svc.imports.create_import(db, data=data, filename=file.filename, resource=resource, fmt=format, consent_basis=consent_basis))


@router.get("/imports", response_model=list[ImportJobOut])
async def find_imports(_: Write, db: PublicDBSessionDep, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    return await svc.imports.find_by_batch(db, batch_id)


@router.get("/imports/{import_id}", response_model=ImportJobOut)
async def get_import(import_id: uuid.UUID, _: Write, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.imports.get_import(db, import_id)


@router.get("/imports/{import_id}/errors", response_class=Response, responses={200: {"content": {"text/csv": {}}}})
async def import_errors(import_id: uuid.UUID, _: Write, db: PublicDBSessionDep) -> Response:
    return Response(await svc.imports.error_report(db, import_id), media_type="text/csv")
