"""Worker "My Work" credentials (Epic 21, ``/api/v1/portal/work/credentials``): the Worker's own credentials
(last 4 digits only), what current Placements still need (labels only), record / renew and their own file.
Worker org scope + explicit ownership: another Worker's credential is a 404."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.compliance import CredentialIn, RenewIn
from app.schemas.compliance_portal import PortalCredentialOut, PortalCredentialsOut
from app.services import files_service
from app.services.compliance import portal

router = APIRouter(prefix="/portal/work/credentials", tags=["portal-work"])


async def _file(file: UploadFile | None) -> tuple[bytes, str | None] | None:
    if file is None:
        return None
    return await files_service.read_upload(file), file.filename


@router.get("", response_model=PortalCredentialsOut)
async def my_credentials(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_mine(db, principal.candidate_id)


@router.post("", response_model=PortalCredentialOut, status_code=status.HTTP_201_CREATED)
async def add_credential(
    principal: WorkerDep,
    db: WorkerDBSessionDep,
    type_key: Annotated[str, Form(min_length=1, max_length=60)],
    number: Annotated[str | None, Form(max_length=60)] = None,
    issuer: Annotated[str | None, Form(max_length=200)] = None,
    issued_on: Annotated[date | None, Form()] = None,
    expires_on: Annotated[date | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> Any:
    data = CredentialIn(
        type_key=type_key, number=number, issuer=issuer, issued_on=issued_on, expires_on=expires_on
    )
    return await portal.record_mine(db, principal.candidate_id, data, await _file(file))


@router.post(
    "/{credential_id}/renew", response_model=PortalCredentialOut, status_code=status.HTTP_201_CREATED
)
async def renew_credential(
    credential_id: uuid.UUID,
    principal: WorkerDep,
    db: WorkerDBSessionDep,
    number: Annotated[str | None, Form(max_length=60)] = None,
    issuer: Annotated[str | None, Form(max_length=200)] = None,
    issued_on: Annotated[date | None, Form()] = None,
    expires_on: Annotated[date | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> Any:
    data = RenewIn(number=number, issuer=issuer, issued_on=issued_on, expires_on=expires_on)
    return await portal.renew_mine(db, principal.candidate_id, credential_id, data, await _file(file))


@router.get("/{credential_id}/document", response_class=Response)
async def my_document(credential_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Response:
    data, name, ctype = await portal.my_document(db, principal.candidate_id, credential_id)
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
