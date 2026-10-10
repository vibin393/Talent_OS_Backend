# ruff: noqa: E501
"""DSC-signed invoice / credit-note PDFs (Epic 51, FR-253).

* ``/dsc/policies`` -- policies per GST Registration and Client (``dsc:configure``; read also with ``billing:read``).
* ``/billing/dsc/pending`` -- the pending-signatures queue; ``/billing/{invoices|credit-notes}/{id}/dsc`` status,
  ``/dsc/render`` (the stored render to sign externally), ``/dsc/upload`` (Manual adapter: the externally signed
  PDF, verified) and ``/dsc/retry`` (``invoice:issue`` or ``dsc:configure``).
Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.dsc import (
    DscPendingOut,
    DscPolicyIn,
    DscPolicyOut,
    DscPolicyPatch,
    DscStatusOut,
    DscUploadOut,
)
from app.services import files_service
from app.services.billing import dsc

router = APIRouter(tags=["dsc"])
DB = TenantDBSessionDep

Configure = Annotated[Principal, Depends(require("dsc:configure"))]
PolicyRead = Annotated[Principal, Depends(require_any("dsc:configure", "billing:read"))]
Signer = Annotated[Principal, Depends(require_any("invoice:issue", "dsc:configure"))]
KindPath = Literal["invoices", "credit-notes"]


# ---------------------------------------------------------------- policies
@router.get("/dsc/policies", response_model=list[DscPolicyOut])
async def list_policies(_: PolicyRead, db: DB) -> Any:
    return await dsc.list_policies(get_context(), db)


@router.post("/dsc/policies", response_model=DscPolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(body: DscPolicyIn, _: Configure, db: DB) -> Any:
    return await dsc.create_policy(get_context(), db, body)


@router.patch("/dsc/policies/{policy_id}", response_model=DscPolicyOut)
async def update_policy(policy_id: uuid.UUID, body: DscPolicyPatch, _: Configure, db: DB) -> Any:
    return await dsc.update_policy(get_context(), db, policy_id, body)


@router.delete("/dsc/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(policy_id: uuid.UUID, _: Configure, db: DB) -> Response:
    await dsc.delete_policy(get_context(), db, policy_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- queue / documents
@router.get("/billing/dsc/pending", response_model=list[DscPendingOut])
async def pending(_: Signer, db: DB) -> Any:
    return await dsc.pending(get_context(), db)


@router.get("/billing/{kind}/{doc_id}/dsc", response_model=DscStatusOut)
async def get_status(kind: KindPath, doc_id: uuid.UUID, _: Signer, db: DB) -> Any:
    return await dsc.status(get_context(), db, dsc.kind_from_path(kind), doc_id)


@router.get("/billing/{kind}/{doc_id}/dsc/render")
async def get_render(kind: KindPath, doc_id: uuid.UUID, _: Signer, db: DB) -> Response:
    data, filename = await dsc.render_for_signing(get_context(), db, dsc.kind_from_path(kind), doc_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/billing/{kind}/{doc_id}/dsc/upload", response_model=DscUploadOut)
async def upload_signed(
    kind: KindPath, doc_id: uuid.UUID, file: Annotated[UploadFile, File()], _: Signer, db: DB
) -> Any:
    data = await files_service.read_upload(file)
    return await dsc.upload_signed(get_context(), db, dsc.kind_from_path(kind), doc_id, data, file.filename)


@router.post("/billing/{kind}/{doc_id}/dsc/retry", response_model=DscStatusOut)
async def retry(kind: KindPath, doc_id: uuid.UUID, _: Signer, db: DB) -> Any:
    return await dsc.retry(get_context(), db, dsc.kind_from_path(kind), doc_id)
