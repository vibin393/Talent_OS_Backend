"""Public secure-link pages of Epic 20 (``/api/v1/public/links/{token}/...``). No session; the token is the
credential and is never logged.

* ``bgv-check`` (purpose ``bgv_provider_check``): GET minimal view, GET one of the check's own files, POST the
  provider's result (a *proposal*; one use).
* ``reference`` (purpose ``reference_check``): GET questionnaire (never mutates), POST answers, POST decline.
"""

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, Response, UploadFile

from app.core.context import get_context
from app.schemas.bgv import ProviderCheckView, ProviderSubmitIn, ReferenceFormView, ReferenceSubmitIn
from app.schemas.pipeline import PublicActionOut
from app.services.bgv import provider_links, references
from app.services.files_service import read_upload

router = APIRouter(prefix="/public/links", tags=["public"])


@router.get("/{token}/bgv-check", response_model=ProviderCheckView)
async def provider_view(token: str) -> Any:
    return await provider_links.view(token)


@router.get("/{token}/bgv-check/files/{file_id}")
async def provider_file(token: str, file_id: uuid.UUID) -> Response:
    data, name, content_type = await provider_links.download(token, file_id)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.post("/{token}/bgv-check", response_model=PublicActionOut)
async def provider_submit(
    token: str,
    result: Annotated[str, Form()],
    note: Annotated[str | None, Form(max_length=4000)] = None,
    verifier_name: Annotated[str | None, Form(max_length=200)] = None,
    source_contacted: Annotated[str | None, Form(max_length=300)] = None,
    verified_on: Annotated[str | None, Form()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> Any:
    data = ProviderSubmitIn.model_validate(
        {
            "result": result,
            "note": note or None,
            "verifier_name": verifier_name or None,
            "source_contacted": source_contacted or None,
            "verified_on": verified_on or None,
        }
    )
    uploads = [(await read_upload(f, 10), f.filename) for f in (files or [])[:10]]
    return PublicActionOut(status=await provider_links.submit(token, data, uploads))


@router.get("/{token}/reference", response_model=ReferenceFormView)
async def reference_view(token: str) -> Any:
    return await references.public_view(token)


@router.post("/{token}/reference", response_model=PublicActionOut)
async def reference_submit(token: str, data: ReferenceSubmitIn, request: Request) -> Any:  # noqa: ARG001
    ctx = get_context()
    ip_hash = hashlib.sha256(ctx.ip.encode()).hexdigest()[:32] if ctx.ip else None
    return PublicActionOut(status=await references.public_submit(token, data.answers, ip_hash))


@router.post("/{token}/reference/decline", response_model=PublicActionOut)
async def reference_decline(token: str) -> Any:
    return PublicActionOut(status=await references.public_decline(token))
