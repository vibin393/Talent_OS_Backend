"""Documents, templates, signature requests, internal signing and verification (Epic 22, ``/api/v1``).

Templates need ``document_template:manage``; documents ``document:read`` / ``document:write`` plus the owning
record's access (``hr`` / ``finance`` documents also ``document:hr`` / ``document:finance``); requests
``signature_request:create`` (cancel: the creator or ``signature_request:cancel``); verification
``document:verify``. Internal signing needs only a session and the signer row. Each endpoint calls one service
function.
"""

import uuid
from datetime import date
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import Response

from app.core import rate_limit
from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.documents import (
    AccessLevel,
    CancelIn,
    CeremonyActionOut,
    CeremonyOut,
    ConsentIn,
    DeclineSignIn,
    DocumentOut,
    DocumentPatchIn,
    MergeCatalogueOut,
    OtpSentOut,
    SignatureRequestIn,
    SignatureRequestOut,
    SignIn,
    TemplateCreateIn,
    TemplateOut,
    TemplatePatchIn,
    TemplateVersionIn,
    TemplateVersionOut,
    VerifyOut,
)
from app.services import files_service
from app.services.documents import esign, repository, service, templates, verify

router = APIRouter(prefix="/documents", tags=["documents"])

Manage = Annotated[Principal, Depends(require("document_template:manage"))]
Read = Annotated[Principal, Depends(require("document:read"))]
Write = Annotated[Principal, Depends(require("document:write"))]
Create = Annotated[Principal, Depends(require("signature_request:create"))]
Verify = Annotated[Principal, Depends(require("document:verify"))]

PDF_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
    "Cache-Control": "private, no-store",
}


def _disposition(filename: str, kind: str = "attachment") -> str:
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in filename) or "file"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _pdf(data: bytes, filename: str, *, inline: bool = True) -> Response:
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            **PDF_HEADERS,
            "Content-Disposition": _disposition(filename, "inline" if inline else "attachment"),
        },
    )


# ================================================================ templates
@router.get("/merge-fields", response_model=MergeCatalogueOut)
async def merge_catalogue(_: Manage) -> Any:
    return templates.catalogue()


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    _: Manage,
    db: TenantDBSessionDep,
    kind: Annotated[str | None, Query()] = None,
    include_archived: bool = False,
) -> Any:
    return await templates.list_templates(get_context(), db, kind=kind, include_archived=include_archived)


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateCreateIn, _: Manage, db: TenantDBSessionDep) -> Any:
    return await templates.create_template(get_context(), db, body)


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(template_id: uuid.UUID, _: Manage, db: TenantDBSessionDep) -> Any:
    return await templates.get_template(get_context(), db, template_id)


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def patch_template(
    template_id: uuid.UUID, body: TemplatePatchIn, _: Manage, db: TenantDBSessionDep
) -> Any:
    return await templates.patch_template(get_context(), db, template_id, body)


@router.delete("/templates/{template_id}", response_model=TemplateOut)
async def archive_template(template_id: uuid.UUID, _: Manage, db: TenantDBSessionDep) -> Any:
    return await templates.patch_template(get_context(), db, template_id, TemplatePatchIn(archived=True))


@router.post(
    "/templates/{template_id}/versions",
    response_model=TemplateVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_template_version(
    template_id: uuid.UUID, body: TemplateVersionIn, _: Manage, db: TenantDBSessionDep
) -> Any:
    return await templates.add_version(get_context(), db, template_id, body.body_md)


@router.post("/templates/versions/{version_id}/publish", response_model=TemplateOut)
async def publish_template_version(version_id: uuid.UUID, _: Manage, db: TenantDBSessionDep) -> Any:
    return await templates.publish_version(get_context(), db, version_id)


@router.post("/templates/versions/{version_id}/preview", response_class=Response)
async def preview_template_version(version_id: uuid.UUID, _: Manage, db: TenantDBSessionDep) -> Response:
    return _pdf(await templates.preview_version(get_context(), db, version_id), "preview.pdf")


# ================================================================ verification
@router.post("/verify", response_model=VerifyOut)
async def verify_document(_: Verify, db: TenantDBSessionDep, file: Annotated[UploadFile, File()]) -> Any:
    ctx = get_context()
    await rate_limit.enforce("document_verify", str(getattr(ctx.principal, "id", "anon")), 30, 3600)
    data = await files_service.read_upload(file)
    return await verify.verify_upload(ctx, db, data)


# ================================================================ signature requests
@router.post("/signature-requests", response_model=SignatureRequestOut, status_code=status.HTTP_201_CREATED)
async def create_signature_request(body: SignatureRequestIn, _: Create, db: TenantDBSessionDep) -> Any:
    return await service.create_from_api(get_context(), db, body)


@router.get("/signature-requests", response_model=list[SignatureRequestOut])
async def list_signature_requests(
    _: Read,
    db: TenantDBSessionDep,
    owner_kind: Annotated[str | None, Query(max_length=40)] = None,
    owner_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status", max_length=20)] = None,
) -> Any:
    return await service.list_requests(
        get_context(), db, owner_kind=owner_kind, owner_id=owner_id, status=status_filter
    )


@router.get("/signature-requests/{request_id}", response_model=SignatureRequestOut)
async def get_signature_request(request_id: uuid.UUID, _: Read, db: TenantDBSessionDep) -> Any:
    return await service.get_request(get_context(), db, request_id)


@router.post("/signature-requests/{request_id}/send", response_model=SignatureRequestOut)
async def send_signature_request(request_id: uuid.UUID, _: Create, db: TenantDBSessionDep) -> Any:
    return await service.send(get_context(), db, request_id)


@router.post("/signature-requests/{request_id}/cancel", response_model=SignatureRequestOut)
async def cancel_signature_request(
    request_id: uuid.UUID, body: CancelIn, _: CurrentUserDep, db: TenantDBSessionDep
) -> Any:
    return await service.cancel(get_context(), db, request_id, body.reason)


# ================================================================ internal signing (tenant signatory)
@router.get("/signers/mine", response_model=list[dict[str, Any]])
async def my_signature_tasks(_: CurrentUserDep, db: TenantDBSessionDep) -> Any:
    return await service.my_pending(get_context(), db)


def _session(db: Any, signer_id: uuid.UUID, request: Request) -> esign.SignerSession:
    ctx = get_context()
    return esign.SignerSession(
        db, signer_id, "session", ctx.ip, request.headers.get("user-agent"), actor_kind="user"
    )


@router.get("/signers/{signer_id}", response_model=CeremonyOut)
async def signer_ceremony(
    signer_id: uuid.UUID, request: Request, _: CurrentUserDep, db: TenantDBSessionDep
) -> Any:
    await service.internal_signer(get_context(), db, signer_id)
    return await _session(db, signer_id, request).ceremony(
        document_url=f"/api/v1/documents/signers/{signer_id}/view"
    )


@router.get("/signers/{signer_id}/view", response_class=Response)
async def signer_view(
    signer_id: uuid.UUID, request: Request, _: CurrentUserDep, db: TenantDBSessionDep
) -> Response:
    await service.internal_signer(get_context(), db, signer_id)
    data, name = await _session(db, signer_id, request).view()
    return _pdf(data, name)


@router.post("/signers/{signer_id}/otp", response_model=OtpSentOut)
async def signer_otp(
    signer_id: uuid.UUID, request: Request, _: CurrentUserDep, db: TenantDBSessionDep
) -> Any:
    await service.internal_signer(get_context(), db, signer_id)
    expires, hint = await _session(db, signer_id, request).request_otp()
    return OtpSentOut(sent=True, expires_at=expires, email_hint=hint)


@router.post("/signers/{signer_id}/consent", response_model=CeremonyActionOut)
async def signer_consent(
    signer_id: uuid.UUID, body: ConsentIn, request: Request, _: CurrentUserDep, db: TenantDBSessionDep
) -> Any:
    await service.internal_signer(get_context(), db, signer_id)
    await _session(db, signer_id, request).consent(body.agreed)
    return CeremonyActionOut(status="consented", request_status="open")


@router.post("/signers/{signer_id}/sign", response_model=CeremonyActionOut)
async def signer_sign(
    signer_id: uuid.UUID, body: SignIn, request: Request, _: CurrentUserDep, db: TenantDBSessionDep
) -> Any:
    await service.internal_signer(get_context(), db, signer_id)
    req = await _session(db, signer_id, request).sign(body.typed_name, body.otp, agreed=body.agreed)
    return CeremonyActionOut(status="signed", request_status=req.status)


@router.post("/signers/{signer_id}/decline", response_model=CeremonyActionOut)
async def signer_decline(
    signer_id: uuid.UUID, body: DeclineSignIn, request: Request, _: CurrentUserDep, db: TenantDBSessionDep
) -> Any:
    await service.internal_signer(get_context(), db, signer_id)
    req = await _session(db, signer_id, request).decline(body.reason)
    return CeremonyActionOut(status="declined", request_status=req.status)


# ================================================================ repository
@router.get("", response_model=list[DocumentOut])
async def list_documents(
    _: Read,
    db: TenantDBSessionDep,
    owner_kind: Annotated[str, Query(min_length=1, max_length=40)],
    owner_id: uuid.UUID,
    include_archived: bool = False,
) -> Any:
    return await repository.list_documents(
        get_context(), db, owner_kind, owner_id, include_archived=include_archived
    )


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    _: Write,
    db: TenantDBSessionDep,
    owner_kind: Annotated[str, Form(min_length=1, max_length=40)],
    owner_id: Annotated[uuid.UUID, Form()],
    category: Annotated[str, Form(min_length=1, max_length=40)],
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=300)] = None,
    access_level: Annotated[AccessLevel, Form()] = "entity",
    expires_on: Annotated[date | None, Form()] = None,
) -> Any:
    ctx = get_context()
    data = await files_service.read_upload(file)
    doc = await repository.upload_document(
        ctx,
        db,
        owner_kind=owner_kind,
        owner_id=owner_id,
        data=data,
        filename=file.filename,
        category=category,
        title=title,
        access_level=access_level,
        expires_on=expires_on,
    )
    return await repository.document_out(db, doc)


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, _: Read, db: TenantDBSessionDep) -> Any:
    return await repository.get_document(get_context(), db, document_id)


@router.patch("/{document_id}", response_model=DocumentOut)
async def patch_document(
    document_id: uuid.UUID, body: DocumentPatchIn, _: Write, db: TenantDBSessionDep
) -> Any:
    return await repository.patch_document(get_context(), db, document_id, body)


@router.delete("/{document_id}", response_model=DocumentOut)
async def archive_document(document_id: uuid.UUID, _: Write, db: TenantDBSessionDep) -> Any:
    return await repository.archive_document(get_context(), db, document_id)


@router.post("/{document_id}/versions", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def add_document_version(
    document_id: uuid.UUID, _: Write, db: TenantDBSessionDep, file: Annotated[UploadFile, File()]
) -> Any:
    data = await files_service.read_upload(file)
    return await repository.add_version(get_context(), db, document_id, data, file.filename)


@router.post("/{document_id}/supersede", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def supersede_document(
    document_id: uuid.UUID, _: Write, db: TenantDBSessionDep, file: Annotated[UploadFile, File()]
) -> Any:
    data = await files_service.read_upload(file)
    return await repository.supersede(get_context(), db, document_id, data, file.filename)


@router.get("/{document_id}/versions/{version_id}/download", response_class=Response)
async def download_document_version(
    document_id: uuid.UUID, version_id: uuid.UUID, _: Read, db: TenantDBSessionDep
) -> Response:
    result = await repository.download_version(get_context(), db, document_id, version_id)
    return Response(
        content=result.data,
        media_type=result.content_type,
        headers={**PDF_HEADERS, "Content-Disposition": _disposition(result.filename)},
    )
