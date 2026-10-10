"""Client commercials (Epic 16, FR-88..FR-92): payment terms, settings, billing profiles, contracts, milestones,
rate cards (+ templates, CSV import), rate checks and purchase orders.

Permissions: payment terms read by any signed-in user, written with ``billing:write``; billing profiles read
with ``client:read`` and written with ``client:update`` or ``billing:write``; contracts ``contract:read|write``;
rate cards / checks ``rate_card:read|write``; POs ``po:read|write``; settings ``contract:read`` /
``settings:manage``. External (client / vendor) principals never reach these routes (AD-26). Each endpoint
calls one service function.
"""  # noqa: E501

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.commercials import (
    BillingAddressIn,
    BillingAddressOut,
    BillingAddressUpdate,
    BillingContactsIn,
    BillingProfileIn,
    BillingProfileOut,
    CommercialsSettingsOut,
    CommercialsSettingsUpdate,
    ContractCreate,
    ContractDocumentKind,
    ContractDocumentOut,
    ContractHistoryItem,
    ContractOut,
    ContractTerminateIn,
    ContractUpdate,
    CopyTemplateIn,
    MilestoneIn,
    MilestoneOut,
    MilestoneUpdate,
    PaymentTermIn,
    PaymentTermOut,
    PaymentTermUpdate,
    PoConsumptionOut,
    PoExceptionIn,
    PoPlacementOut,
    PurchaseOrderIn,
    PurchaseOrderOut,
    PurchaseOrderUpdate,
    RateCardImportOut,
    RateCardIn,
    RateCardLinesIn,
    RateCardOut,
    RateCardTemplateIn,
    RateCardUpdate,
    RateCheckOut,
    RateCheckPreviewOut,
    RateCheckRequest,
)
from app.services.commercials import (
    billing_profiles,
    consumption,
    contracts,
    milestones,
    payment_terms,
    purchase_orders,
    rate_cards,
    rate_checks,
)
from app.services.commercials import settings as commercials_settings

router = APIRouter(tags=["commercials"])
DB = TenantDBSessionDep

BillingWrite = Annotated[Principal, Depends(require("billing:write"))]
ClientRead = Annotated[Principal, Depends(require("client:read"))]
ProfileWrite = Annotated[Principal, Depends(require_any("client:update", "billing:write"))]
ContractRead = Annotated[Principal, Depends(require("contract:read"))]
ContractWrite = Annotated[Principal, Depends(require("contract:write"))]
SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]
RateRead = Annotated[Principal, Depends(require("rate_card:read"))]
RateWrite = Annotated[Principal, Depends(require("rate_card:write"))]
PoRead = Annotated[Principal, Depends(require("po:read"))]
PoWrite = Annotated[Principal, Depends(require("po:write"))]


class SignerIn(BaseModel):
    kind: str = "contact"
    ref_id: uuid.UUID | None = None
    email: str
    order: int = 1
    name: str | None = None


class SignatureRequestIn(BaseModel):
    document_file_id: uuid.UUID
    signers: list[SignerIn] = Field(min_length=1, max_length=10)


class ApprovalRequestRef(BaseModel):
    approval_request_id: uuid.UUID


# ================================================================ payment terms / settings
@router.get("/payment-terms", response_model=list[PaymentTermOut])
async def list_payment_terms(_: CurrentUserDep, db: DB, include_archived: bool = False) -> Any:
    return await payment_terms.list_terms(get_context(), db, include_archived=include_archived)


@router.post("/payment-terms", response_model=PaymentTermOut, status_code=status.HTTP_201_CREATED)
async def create_payment_terms(body: PaymentTermIn, _: BillingWrite, db: DB) -> Any:
    return await payment_terms.create_term(get_context(), db, body)


@router.patch("/payment-terms/{term_id}", response_model=PaymentTermOut)
async def update_payment_terms(term_id: uuid.UUID, body: PaymentTermUpdate, _: BillingWrite, db: DB) -> Any:
    return await payment_terms.update_term(get_context(), db, term_id, body)


@router.post("/payment-terms/{term_id}/archive", response_model=PaymentTermOut)
async def archive_payment_terms(term_id: uuid.UUID, _: BillingWrite, db: DB) -> Any:
    return await payment_terms.archive_term(get_context(), db, term_id)


@router.get("/commercials/settings", response_model=CommercialsSettingsOut)
async def get_commercials_settings(_: ContractRead, db: DB) -> Any:
    return await commercials_settings.get_settings(get_context(), db)


@router.patch("/commercials/settings", response_model=CommercialsSettingsOut)
async def update_commercials_settings(
    body: CommercialsSettingsUpdate, _: SettingsManage, __: ContractWrite, db: DB
) -> Any:
    return await commercials_settings.update_settings(get_context(), db, body)


# ================================================================ billing profile
@router.get("/clients/{client_id}/billing-profile", response_model=BillingProfileOut | None)
async def get_billing_profile(client_id: uuid.UUID, _: ClientRead, db: DB) -> Any:
    return await billing_profiles.get_profile(get_context(), db, client_id)


@router.put("/clients/{client_id}/billing-profile", response_model=BillingProfileOut)
async def put_billing_profile(client_id: uuid.UUID, body: BillingProfileIn, _: ProfileWrite, db: DB) -> Any:
    return await billing_profiles.put_profile(get_context(), db, client_id, body)


@router.put("/clients/{client_id}/billing-profile/contacts", response_model=BillingProfileOut)
async def put_billing_contacts(client_id: uuid.UUID, body: BillingContactsIn, _: ProfileWrite, db: DB) -> Any:
    return await billing_profiles.put_contacts(get_context(), db, client_id, body)


@router.post(
    "/clients/{client_id}/billing-addresses",
    response_model=BillingAddressOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_billing_address(client_id: uuid.UUID, body: BillingAddressIn, _: ProfileWrite, db: DB) -> Any:
    return await billing_profiles.add_address(get_context(), db, client_id, body)


@router.patch("/billing-addresses/{address_id}", response_model=BillingAddressOut)
async def update_billing_address(
    address_id: uuid.UUID, body: BillingAddressUpdate, _: ProfileWrite, db: DB
) -> Any:
    return await billing_profiles.update_address(get_context(), db, address_id, body)


@router.post("/billing-addresses/{address_id}/archive", response_model=BillingAddressOut)
async def archive_billing_address(address_id: uuid.UUID, _: ProfileWrite, db: DB) -> Any:
    return await billing_profiles.archive_address(get_context(), db, address_id)


# ================================================================ contracts
@router.get("/contracts", response_model=list[ContractOut])
async def list_contracts(
    _: ContractRead,
    db: DB,
    client_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    type: Annotated[str | None, Query(alias="type")] = None,
    contract_status: Annotated[str | None, Query(alias="status")] = None,
    expiring_within_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
) -> Any:
    return await contracts.list_contracts(
        get_context(),
        db,
        client_id=client_id,
        vendor_id=vendor_id,
        contract_type=type,
        status=contract_status,
        expiring_within_days=expiring_within_days,
    )


@router.get("/clients/{client_id}/contracts", response_model=list[ContractOut])
async def list_client_contracts(client_id: uuid.UUID, _: ContractRead, db: DB) -> Any:
    from app.services.commercials.common import require_client

    await require_client(db, client_id)
    return await contracts.list_contracts(get_context(), db, client_id=client_id)


@router.post("/contracts", response_model=ContractOut, status_code=status.HTTP_201_CREATED)
async def create_contract(body: ContractCreate, _: ContractWrite, db: DB) -> Any:
    return await contracts.create_contract(get_context(), db, body)


@router.get("/contracts/{contract_id}", response_model=ContractOut)
async def get_contract(contract_id: uuid.UUID, _: ContractRead, db: DB) -> Any:
    return await contracts.get_contract_out(get_context(), db, contract_id)


@router.patch("/contracts/{contract_id}", response_model=ContractOut)
async def update_contract(contract_id: uuid.UUID, body: ContractUpdate, _: ContractWrite, db: DB) -> Any:
    return await contracts.update_contract(get_context(), db, contract_id, body)


@router.post("/contracts/{contract_id}/submit-for-approval", response_model=ContractOut)
async def submit_contract(contract_id: uuid.UUID, _: ContractWrite, db: DB) -> Any:
    return await contracts.submit_for_approval(get_context(), db, contract_id)


@router.post("/contracts/{contract_id}/activate", response_model=ContractOut)
async def activate_contract(contract_id: uuid.UUID, _: ContractWrite, db: DB) -> Any:
    return await contracts.activate(get_context(), db, contract_id)


@router.post("/contracts/{contract_id}/terminate", response_model=ContractOut)
async def terminate_contract(
    contract_id: uuid.UUID, body: ContractTerminateIn, _: ContractWrite, db: DB
) -> Any:
    return await contracts.terminate(get_context(), db, contract_id, body.reason, body.termination_date)


@router.post("/contracts/{contract_id}/signature-request", response_model=ContractOut)
async def request_contract_signature(
    contract_id: uuid.UUID, body: SignatureRequestIn, _: ContractWrite, db: DB
) -> Any:
    return await contracts.request_signature(
        get_context(), db, contract_id, [s.model_dump() for s in body.signers], body.document_file_id
    )


@router.get("/contracts/{contract_id}/history", response_model=list[ContractHistoryItem])
async def contract_history(contract_id: uuid.UUID, _: ContractRead, db: DB) -> Any:
    return await contracts.history(get_context(), db, contract_id)


@router.get("/contracts/{contract_id}/documents", response_model=list[ContractDocumentOut])
async def list_contract_documents(contract_id: uuid.UUID, _: ContractRead, db: DB) -> Any:
    return await contracts.list_documents(get_context(), db, contract_id)


@router.post(
    "/contracts/{contract_id}/documents",
    response_model=ContractDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_contract_document(
    contract_id: uuid.UUID,
    _: ContractWrite,
    db: DB,
    file: Annotated[UploadFile, File()],
    kind: Annotated[ContractDocumentKind, Form()] = "draft",
) -> Any:
    from app.services import files_service

    data = await files_service.read_upload(file)
    return await contracts.add_document(get_context(), db, contract_id, data, file.filename, kind)


@router.post("/contract-documents/{document_id}/mark-signed", response_model=ContractDocumentOut)
async def mark_contract_document_signed(document_id: uuid.UUID, _: ContractWrite, db: DB) -> Any:
    return await contracts.mark_document_signed(get_context(), db, document_id)


@router.delete("/contract-documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contract_document(document_id: uuid.UUID, _: ContractWrite, db: DB) -> Response:
    await contracts.delete_document(get_context(), db, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/contracts/{contract_id}/milestones", response_model=list[MilestoneOut])
async def list_contract_milestones(contract_id: uuid.UUID, _: ContractRead, db: DB) -> Any:
    return await milestones.list_milestones(get_context(), db, contract_id)


@router.post(
    "/contracts/{contract_id}/milestones", response_model=MilestoneOut, status_code=status.HTTP_201_CREATED
)
async def add_contract_milestone(contract_id: uuid.UUID, body: MilestoneIn, _: ContractWrite, db: DB) -> Any:
    return await milestones.add_milestone(get_context(), db, contract_id, body)


@router.patch("/contract-milestones/{milestone_id}", response_model=MilestoneOut)
async def update_contract_milestone(
    milestone_id: uuid.UUID, body: MilestoneUpdate, _: ContractWrite, db: DB
) -> Any:
    return await milestones.update_milestone(get_context(), db, milestone_id, body)


@router.post("/contract-milestones/{milestone_id}/accept", response_model=MilestoneOut)
async def accept_contract_milestone(milestone_id: uuid.UUID, _: ContractWrite, db: DB) -> Any:
    return await milestones.accept_milestone(get_context(), db, milestone_id)


@router.post("/contract-milestones/{milestone_id}/cancel", response_model=MilestoneOut)
async def cancel_contract_milestone(milestone_id: uuid.UUID, _: ContractWrite, db: DB) -> Any:
    return await milestones.cancel_milestone(get_context(), db, milestone_id)


# ================================================================ rate cards
@router.get("/clients/{client_id}/rate-cards", response_model=list[RateCardOut])
async def list_client_rate_cards(client_id: uuid.UUID, _: RateRead, db: DB) -> Any:
    return await rate_cards.list_client_cards(get_context(), db, client_id)


@router.get("/clients/{client_id}/rate-cards/effective", response_model=RateCardOut | None)
async def effective_rate_card(
    client_id: uuid.UUID, _: RateRead, db: DB, on: date | None = None, contract_id: uuid.UUID | None = None
) -> Any:
    from app.services.commercials.common import le_today

    return await rate_cards.get_effective(
        get_context(), db, client_id, on or await le_today(db, None), contract_id
    )


@router.post("/rate-cards", response_model=RateCardOut, status_code=status.HTTP_201_CREATED)
async def create_rate_card(body: RateCardIn, _: RateWrite, db: DB) -> Any:
    return await rate_cards.create_card(get_context(), db, body)


@router.get("/rate-cards/{card_id}", response_model=RateCardOut)
async def get_rate_card(card_id: uuid.UUID, _: RateRead, db: DB) -> Any:
    return await rate_cards.get_card(get_context(), db, card_id)


@router.patch("/rate-cards/{card_id}", response_model=RateCardOut)
async def update_rate_card(card_id: uuid.UUID, body: RateCardUpdate, _: RateWrite, db: DB) -> Any:
    return await rate_cards.update_card(get_context(), db, card_id, body)


@router.put("/rate-cards/{card_id}/lines", response_model=RateCardOut)
async def put_rate_card_lines(card_id: uuid.UUID, body: RateCardLinesIn, _: RateWrite, db: DB) -> Any:
    return await rate_cards.put_lines(get_context(), db, card_id, body.lines)


@router.post("/rate-cards/{card_id}/publish", response_model=RateCardOut)
async def publish_rate_card(card_id: uuid.UUID, _: RateWrite, db: DB) -> Any:
    return await rate_cards.publish(get_context(), db, card_id)


@router.post("/rate-cards/{card_id}/archive", response_model=RateCardOut)
async def archive_rate_card(card_id: uuid.UUID, _: RateWrite, db: DB) -> Any:
    return await rate_cards.archive(get_context(), db, card_id)


@router.post(
    "/rate-cards/{card_id}/new-version", response_model=RateCardOut, status_code=status.HTTP_201_CREATED
)
async def new_rate_card_version(card_id: uuid.UUID, _: RateWrite, db: DB) -> Any:
    return await rate_cards.new_version(get_context(), db, card_id)


@router.post("/rate-cards/{card_id}/import", response_model=RateCardImportOut)
async def import_rate_card(
    card_id: uuid.UUID,
    _: RateWrite,
    db: DB,
    file: Annotated[UploadFile, File()],
    skip_invalid: Annotated[bool, Form()] = False,
) -> Any:
    from app.services import files_service

    data = await files_service.read_upload(file, limit_mb=5)
    return await rate_cards.import_csv(get_context(), db, card_id, data, skip_invalid=skip_invalid)


@router.get("/rate-card-templates", response_model=list[RateCardOut])
async def list_rate_card_templates(_: RateRead, db: DB) -> Any:
    return await rate_cards.list_templates(get_context(), db)


@router.post("/rate-card-templates", response_model=RateCardOut, status_code=status.HTTP_201_CREATED)
async def create_rate_card_template(body: RateCardTemplateIn, _: RateWrite, db: DB) -> Any:
    return await rate_cards.create_template(get_context(), db, body)


@router.post(
    "/rate-card-templates/{template_id}/copy", response_model=RateCardOut, status_code=status.HTTP_201_CREATED
)
async def copy_rate_card_template(template_id: uuid.UUID, body: CopyTemplateIn, _: RateWrite, db: DB) -> Any:
    return await rate_cards.copy_template(get_context(), db, template_id, body)


# ================================================================ rate checks
@router.get("/rate-checks", response_model=list[RateCheckOut])
async def list_rate_checks(subject_kind: str, subject_id: uuid.UUID, _: RateRead, db: DB) -> Any:
    return await rate_checks.history(get_context(), db, subject_kind, subject_id)


@router.post("/rate-checks/preview", response_model=RateCheckPreviewOut)
async def preview_rate_check(body: RateCheckRequest, _: CurrentUserDep, db: DB) -> Any:
    return await rate_checks.preview(get_context(), db, body)


# ================================================================ purchase orders
@router.get("/purchase-orders", response_model=list[PurchaseOrderOut])
async def list_purchase_orders(
    _: PoRead,
    db: DB,
    client_id: uuid.UUID | None = None,
    po_status: Annotated[str | None, Query(alias="status")] = None,
    expiring_within_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    over_pct: Annotated[int | None, Query(ge=0, le=1000)] = None,
) -> Any:
    return await purchase_orders.list_pos(
        get_context(),
        db,
        client_id=client_id,
        status=po_status,
        expiring_within_days=expiring_within_days,
        over_pct=over_pct,
    )


@router.get("/clients/{client_id}/purchase-orders", response_model=list[PurchaseOrderOut])
async def list_client_purchase_orders(client_id: uuid.UUID, _: PoRead, db: DB) -> Any:
    from app.services.commercials.common import require_client

    await require_client(db, client_id)
    return await purchase_orders.list_pos(get_context(), db, client_id=client_id)


@router.post("/purchase-orders", response_model=PurchaseOrderOut, status_code=status.HTTP_201_CREATED)
async def create_purchase_order(body: PurchaseOrderIn, _: PoWrite, db: DB) -> Any:
    return await purchase_orders.create_po(get_context(), db, body)


@router.get("/purchase-orders/{po_id}", response_model=PurchaseOrderOut)
async def get_purchase_order(po_id: uuid.UUID, _: PoRead, db: DB) -> Any:
    return await purchase_orders.get_po(get_context(), db, po_id)


@router.patch("/purchase-orders/{po_id}", response_model=PurchaseOrderOut)
async def update_purchase_order(po_id: uuid.UUID, body: PurchaseOrderUpdate, _: PoWrite, db: DB) -> Any:
    return await purchase_orders.update_po(get_context(), db, po_id, body)


@router.post("/purchase-orders/{po_id}/close", response_model=PurchaseOrderOut)
async def close_purchase_order(po_id: uuid.UUID, _: PoWrite, db: DB) -> Any:
    return await purchase_orders.close_po(get_context(), db, po_id)


@router.post("/purchase-orders/{po_id}/cancel", response_model=PurchaseOrderOut)
async def cancel_purchase_order(po_id: uuid.UUID, _: PoWrite, db: DB) -> Any:
    return await purchase_orders.cancel_po(get_context(), db, po_id)


@router.post("/purchase-orders/{po_id}/document", response_model=PurchaseOrderOut)
async def upload_purchase_order_document(
    po_id: uuid.UUID, _: PoWrite, db: DB, file: Annotated[UploadFile, File()]
) -> Any:
    from app.services import files_service

    data = await files_service.read_upload(file)
    return await purchase_orders.attach_file(get_context(), db, po_id, data, file.filename)


@router.get("/purchase-orders/{po_id}/consumptions", response_model=list[PoConsumptionOut])
async def list_purchase_order_consumptions(po_id: uuid.UUID, _: PoRead, db: DB) -> Any:
    return await purchase_orders.list_consumptions(get_context(), db, po_id)


@router.get("/purchase-orders/{po_id}/placements", response_model=list[PoPlacementOut])
async def list_purchase_order_placements(po_id: uuid.UUID, _: PoRead, db: DB) -> Any:
    return await purchase_orders.list_placements(get_context(), db, po_id)


@router.post(
    "/purchase-orders/{po_id}/exceptions",
    response_model=ApprovalRequestRef,
    status_code=status.HTTP_201_CREATED,
)
async def request_purchase_order_exception(po_id: uuid.UUID, body: PoExceptionIn, _: PoWrite, db: DB) -> Any:
    request_id = await consumption.request_po_exception(
        get_context(),
        db,
        po_id,
        body.source_doc_kind,
        body.source_doc_id,
        body.amount,
        body.hours,
        body.reason,
    )
    return ApprovalRequestRef(approval_request_id=request_id)
