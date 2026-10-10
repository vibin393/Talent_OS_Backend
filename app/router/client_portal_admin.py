"""Internal endpoints of the Client Portal epic (Epic 18) and the public ``client_approval`` Secure Link page.

* ``POST /applications/{id}/slot-offers`` / ``GET`` (``interview:schedule``): recruiter offers slots to the client.
* ``/clients/{id}/compliance-packs*`` (``document:write`` or ``onboarding:manage``): create, add items, publish,
  download log.
* ``/public/links/{token}/client-approval`` (GET view, POST decide): Client User approval by email link.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.client_portal_admin import (
    CompliancePackDocumentIn,
    CompliancePackIn,
    CompliancePackInternalOut,
    SlotOfferIn,
    SlotProposalInternalOut,
)
from app.schemas.pipeline import PublicActionOut
from app.services.client_portal import compliance, links
from app.services.client_portal_base import ip_hash
from app.services.pipeline import portal as pipeline_portal

router = APIRouter(tags=["client-portal-admin"])
public_router = APIRouter(prefix="/public/links", tags=["public"])
Scheduler = Annotated[Principal, Depends(require("interview:schedule"))]
PackWriter = Annotated[Principal, Depends(require_any("document:write", "onboarding:manage"))]


@router.post(
    "/applications/{application_id}/slot-offers",
    response_model=SlotProposalInternalOut,
    status_code=status.HTTP_201_CREATED,
)
async def offer_slots(
    application_id: uuid.UUID, data: SlotOfferIn, _: Scheduler, db: TenantDBSessionDep
) -> Any:
    return await pipeline_portal.offer_slots(get_context(), db, application_id, data.slots)


@router.get("/applications/{application_id}/slot-proposals", response_model=list[SlotProposalInternalOut])
async def slot_proposals(application_id: uuid.UUID, _: Scheduler, db: TenantDBSessionDep) -> Any:
    return await pipeline_portal.list_slot_proposals(get_context(), db, application_id)


@router.get("/clients/{client_id}/compliance-packs", response_model=list[CompliancePackInternalOut])
async def list_packs(client_id: uuid.UUID, _: PackWriter, db: TenantDBSessionDep) -> Any:
    return await compliance.list_packs(get_context(), db, client_id)


@router.post(
    "/clients/{client_id}/compliance-packs",
    response_model=CompliancePackInternalOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_pack(
    client_id: uuid.UUID, data: CompliancePackIn, _: PackWriter, db: TenantDBSessionDep
) -> Any:
    return await compliance.create_pack(get_context(), db, client_id, data)


@router.post(
    "/clients/{client_id}/compliance-packs/{pack_id}/items", response_model=CompliancePackInternalOut
)
async def add_item(
    client_id: uuid.UUID,
    pack_id: uuid.UUID,
    _: PackWriter,
    db: TenantDBSessionDep,
    file: Annotated[UploadFile, File()],
    label: Annotated[str | None, Form(max_length=200)] = None,
) -> Any:
    from app.services import files_service

    data = await files_service.read_upload(file)
    return await compliance.add_file_item(get_context(), db, client_id, pack_id, data, file.filename, label)


@router.post(
    "/clients/{client_id}/compliance-packs/{pack_id}/documents", response_model=CompliancePackInternalOut
)
async def add_document(
    client_id: uuid.UUID,
    pack_id: uuid.UUID,
    data: CompliancePackDocumentIn,
    _: PackWriter,
    db: TenantDBSessionDep,
) -> Any:
    return await compliance.add_document_item(
        get_context(), db, client_id, pack_id, data.document_id, data.label
    )


@router.post(
    "/clients/{client_id}/compliance-packs/{pack_id}/publish", response_model=CompliancePackInternalOut
)
async def publish_pack(
    client_id: uuid.UUID, pack_id: uuid.UUID, _: PackWriter, db: TenantDBSessionDep
) -> Any:
    return await compliance.publish(get_context(), db, client_id, pack_id)


# ---------------------------------------------------------------- public secure link
class _Field(BaseModel):
    label: str
    value: str | None


class ClientApprovalLinkView(BaseModel):
    kind: str
    status: str
    title: str
    subtitle: str | None
    fields: list[_Field]
    tenant_name: str | None
    expires_at: Any


class ClientApprovalLinkDecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=4000)


@public_router.get("/{token}/client-approval", response_model=ClientApprovalLinkView)
async def link_view(token: str) -> Any:
    return await links.view(token)


@public_router.post("/{token}/client-approval/decide", response_model=PublicActionOut)
async def link_decide(token: str, data: ClientApprovalLinkDecideIn) -> Any:
    return PublicActionOut(
        status=await links.decide(token, data.decision, data.comment, ip_hash=ip_hash(get_context().ip))
    )
