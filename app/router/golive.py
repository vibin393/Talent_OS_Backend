"""Go-live data import (Epic 35 A8, FR-185): ``/api/v1/golive``. Every route needs ``golive:import``.

Upload a CSV per kind (validated inline through the owner functions' ``validate_only`` dry runs), preview the
row errors, download the masked error CSV, then apply (``Idempotency-Key``) or discard.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.core.settings import get_settings
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.golive import (
    GoLiveApplyIn,
    GoLiveColumnOut,
    GoLiveImportOut,
    GoLiveKind,
    GoLiveKindOut,
    GoLiveRowOut,
    GoLiveRowStatus,
)
from app.services import files_service, golive

router = APIRouter(prefix="/golive", tags=["golive"])
DB = TenantDBSessionDep
Import = Annotated[Principal, Depends(require("golive:import"))]


@router.get("/kinds", response_model=list[GoLiveKindOut])
async def kinds(_: Import) -> Any:
    return golive.list_kinds()


@router.get("/templates/{kind}", response_class=Response, responses={200: {"content": {"text/csv": {}}}})
async def template(kind: str, _: Import) -> Response:
    body = golive.templates.template_csv(kind)
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="golive-{kind}-template.csv"'},
    )


@router.get("/templates/{kind}/dictionary", response_model=list[GoLiveColumnOut])
async def template_dictionary(kind: str, _: Import) -> Any:
    return golive.templates.dictionary(kind)


@router.post("/imports", response_model=GoLiveImportOut, status_code=201)
async def upload(
    _: Import, db: DB, kind: Annotated[GoLiveKind, Form()], file: Annotated[UploadFile, File()]
) -> Any:
    data = await files_service.read_upload(file, limit_mb=get_settings().golive_max_file_mb)
    out, created = await golive.upload(get_context(), db, kind, file.filename, data)
    if created:
        return out
    return JSONResponse(content=jsonable_encoder(GoLiveImportOut.model_validate(out)), status_code=200)


@router.get("/imports", response_model=list[GoLiveImportOut])
async def list_imports(_: Import, db: DB, kind: GoLiveKind | None = None) -> Any:
    return await golive.list_imports(get_context(), db, kind)


@router.get("/imports/{batch_id}", response_model=GoLiveImportOut)
async def get_import(batch_id: uuid.UUID, _: Import, db: DB) -> Any:
    return await golive.get_import(get_context(), db, batch_id)


@router.get(
    "/imports/{batch_id}/errors.csv", response_class=Response, responses={200: {"content": {"text/csv": {}}}}
)
async def errors_csv(batch_id: uuid.UUID, _: Import, db: DB) -> Response:
    body = await golive.errors_csv(get_context(), db, batch_id)
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="golive-{batch_id}-errors.csv"'},
    )


@router.get("/imports/{batch_id}/rows", response_model=list[GoLiveRowOut])
async def rows(
    batch_id: uuid.UUID, _: Import, db: DB, status: Annotated[GoLiveRowStatus | None, Query()] = None
) -> Any:
    return await golive.list_rows(get_context(), db, batch_id, status)


@router.post("/imports/{batch_id}/apply", response_model=GoLiveImportOut)
async def apply(
    batch_id: uuid.UUID,
    data: GoLiveApplyIn,
    _: Import,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    return await idem.run(
        lambda: golive.apply(
            get_context(), db, batch_id, skip_invalid=data.skip_invalid, confirm=data.confirm
        )
    )


@router.post("/imports/{batch_id}/discard", response_model=GoLiveImportOut)
async def discard(batch_id: uuid.UUID, _: Import, db: DB) -> Any:
    return await golive.discard(get_context(), db, batch_id)
