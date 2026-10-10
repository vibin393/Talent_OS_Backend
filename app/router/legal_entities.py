"""Legal Entities, GST registrations, bank accounts, numbering series, periods and FX rates (Epic 13, AD-17).

Reads need ``legal_entity:read``; writes ``legal_entity:write``; series ``document_series:write``; FX rates
``fx_rate:write``. Bank account numbers are returned masked (last four). Each endpoint calls one service
function.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Path, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.legal_entities import (
    AccountingPeriodOut,
    BankAccountIn,
    BankAccountOut,
    BankAccountUnmaskedOut,
    BankAccountUpdate,
    CurrencyOut,
    DocumentSeriesIn,
    DocumentSeriesOut,
    DocumentSeriesUpdate,
    FxRateIn,
    FxRateOut,
    GstRegistrationIn,
    GstRegistrationOut,
    GstRegistrationUpdate,
    GstStateOut,
    LegalEntityDeactivateIn,
    LegalEntityIn,
    LegalEntityOut,
    LegalEntityReadinessOut,
    LegalEntityUpdate,
    LutIn,
    LutOut,
    LutPutIn,
)
from app.services import legal_entities_service as svc

router = APIRouter(tags=["legal-entities"])

Read = Annotated[Principal, Depends(require("legal_entity:read"))]
Write = Annotated[Principal, Depends(require("legal_entity:write"))]
SeriesWrite = Annotated[Principal, Depends(require("document_series:write"))]
FxWrite = Annotated[Principal, Depends(require("fx_rate:write"))]
IdentityView = Annotated[Principal, Depends(require("identity:view"))]
DB = TenantDBSessionDep


# ---------------------------------------------------------------- entities
@router.get("/legal-entities", response_model=list[LegalEntityOut])
async def list_entities(_: Read, db: DB) -> Any:
    return await svc.list_entities(get_context(), db)


@router.post("/legal-entities", response_model=LegalEntityOut, status_code=status.HTTP_201_CREATED)
async def create_entity(body: LegalEntityIn, _: Write, db: DB) -> Any:
    return await svc.create_entity(get_context(), db, body)


@router.get("/legal-entities/{legal_entity_id}", response_model=LegalEntityOut)
async def get_entity(legal_entity_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_entity(get_context(), db, legal_entity_id)


@router.patch("/legal-entities/{legal_entity_id}", response_model=LegalEntityOut)
async def update_entity(legal_entity_id: uuid.UUID, body: LegalEntityUpdate, _: Write, db: DB) -> Any:
    return await svc.update_entity(get_context(), db, legal_entity_id, body)


@router.delete("/legal-entities/{legal_entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entity(legal_entity_id: uuid.UUID, _: Write, db: DB) -> Response:
    await svc.delete_entity(get_context(), db, legal_entity_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/legal-entities/{legal_entity_id}/deactivate", response_model=LegalEntityOut)
async def deactivate_entity(
    legal_entity_id: uuid.UUID, _: Write, db: DB, body: LegalEntityDeactivateIn | None = None
) -> Any:
    return await svc.deactivate(get_context(), db, legal_entity_id, body.reason if body is not None else None)


@router.post("/legal-entities/{legal_entity_id}/activate", response_model=LegalEntityOut)
async def activate_entity(legal_entity_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await svc.activate(get_context(), db, legal_entity_id)


# ---------------------------------------------------------------- GST registrations / LUTs
@router.get("/legal-entities/{legal_entity_id}/gst-registrations", response_model=list[GstRegistrationOut])
async def list_registrations(legal_entity_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_registrations(get_context(), db, legal_entity_id)


@router.post(
    "/legal-entities/{legal_entity_id}/gst-registrations",
    response_model=GstRegistrationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_registration(legal_entity_id: uuid.UUID, body: GstRegistrationIn, _: Write, db: DB) -> Any:
    return await svc.create_registration(get_context(), db, legal_entity_id, body)


@router.patch("/gst-registrations/{gst_registration_id}", response_model=GstRegistrationOut)
async def update_registration(
    gst_registration_id: uuid.UUID, body: GstRegistrationUpdate, _: Write, db: DB
) -> Any:
    return await svc.update_registration(get_context(), db, gst_registration_id, body)


@router.post(
    "/gst-registrations/{gst_registration_id}/luts",
    response_model=LutOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_lut(gst_registration_id: uuid.UUID, body: LutIn, _: Write, db: DB) -> Any:
    return await svc.add_lut(get_context(), db, gst_registration_id, body)


# ---------------------------------------------------------------- bank accounts
@router.get("/legal-entities/{legal_entity_id}/bank-accounts", response_model=list[BankAccountOut])
async def list_bank_accounts(legal_entity_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_bank_accounts(get_context(), db, legal_entity_id)


@router.post(
    "/legal-entities/{legal_entity_id}/bank-accounts",
    response_model=BankAccountOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_bank_account(legal_entity_id: uuid.UUID, body: BankAccountIn, _: Write, db: DB) -> Any:
    return await svc.create_bank_account(get_context(), db, legal_entity_id, body)


# ---------------------------------------------------------------- numbering series
@router.get("/legal-entities/{legal_entity_id}/document-series", response_model=list[DocumentSeriesOut])
async def list_series(legal_entity_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_series(get_context(), db, legal_entity_id)


@router.post(
    "/legal-entities/{legal_entity_id}/document-series",
    response_model=DocumentSeriesOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_series(legal_entity_id: uuid.UUID, body: DocumentSeriesIn, _: SeriesWrite, db: DB) -> Any:
    return await svc.create_series(get_context(), db, legal_entity_id, body)


@router.patch("/document-series/{series_id}", response_model=DocumentSeriesOut)
async def update_series(series_id: uuid.UUID, body: DocumentSeriesUpdate, _: SeriesWrite, db: DB) -> Any:
    return await svc.update_series(get_context(), db, series_id, body)


# ---------------------------------------------------------------- periods
@router.get("/legal-entities/{legal_entity_id}/periods", response_model=list[AccountingPeriodOut])
async def list_periods(
    legal_entity_id: uuid.UUID, _: Read, db: DB, year: Annotated[int | None, Query(ge=2000, le=2200)] = None
) -> Any:
    return await svc.list_entity_periods(get_context(), db, legal_entity_id, year)


# ---------------------------------------------------------------- FX rates
@router.get("/fx-rates", response_model=list[FxRateOut])
async def list_fx_rates(
    _: Read,
    db: DB,
    from_currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    to_currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
) -> Any:
    return await svc.list_rates(get_context(), db, from_currency=from_currency, to_currency=to_currency)


@router.post("/fx-rates", response_model=FxRateOut, status_code=status.HTTP_201_CREATED)
async def upsert_fx_rate(body: FxRateIn, _: FxWrite, db: DB) -> Any:
    return await svc.upsert_rate(get_context(), db, body)


# ---------------------------------------------------------------- Epic 14: administration deltas (FR-76)
@router.post("/legal-entities/{legal_entity_id}/reactivate", response_model=LegalEntityOut)
async def reactivate_entity(legal_entity_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await svc.activate(get_context(), db, legal_entity_id)


@router.get("/legal-entities/{legal_entity_id}/readiness", response_model=LegalEntityReadinessOut)
async def entity_readiness(legal_entity_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.readiness(get_context(), db, legal_entity_id)


@router.post("/legal-entities/{legal_entity_id}/logo", response_model=LegalEntityOut)
async def upload_logo(
    legal_entity_id: uuid.UUID, file: Annotated[UploadFile, File()], _: Write, db: DB
) -> Any:
    return await svc.upload_logo(get_context(), db, legal_entity_id, file)


@router.post("/gst-registrations/{gst_registration_id}/deactivate", response_model=GstRegistrationOut)
async def deactivate_registration(gst_registration_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await svc.set_registration_status(get_context(), db, gst_registration_id, active=False)


@router.post("/gst-registrations/{gst_registration_id}/reactivate", response_model=GstRegistrationOut)
async def reactivate_registration(gst_registration_id: uuid.UUID, _: Write, db: DB) -> Any:
    return await svc.set_registration_status(get_context(), db, gst_registration_id, active=True)


@router.get("/gst-registrations/{gst_registration_id}/luts", response_model=list[LutOut])
async def list_luts(gst_registration_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.list_luts(get_context(), db, gst_registration_id)


@router.put("/gst-registrations/{gst_registration_id}/luts/{fiscal_year}", response_model=LutOut)
async def put_lut(
    gst_registration_id: uuid.UUID,
    fiscal_year: Annotated[str, Path(pattern=r"^[0-9]{2}-[0-9]{2}$")],
    body: LutPutIn,
    _: Write,
    db: DB,
) -> Any:
    return await svc.put_lut(get_context(), db, gst_registration_id, fiscal_year, body)


@router.patch("/bank-accounts/{bank_account_id}", response_model=BankAccountOut)
async def update_bank_account(bank_account_id: uuid.UUID, body: BankAccountUpdate, _: Write, db: DB) -> Any:
    return await svc.update_bank_account(get_context(), db, bank_account_id, body)


@router.post("/bank-accounts/{bank_account_id}/unmask", response_model=BankAccountUnmaskedOut)
async def unmask_bank_account(bank_account_id: uuid.UUID, _: IdentityView, db: DB) -> Any:
    return await svc.unmask_bank_account(get_context(), db, bank_account_id)


@router.get("/reference/gst-states", response_model=list[GstStateOut])
async def gst_states(_: Read, db: DB) -> Any:
    return await svc.list_gst_states(get_context(), db)


@router.get("/reference/currencies", response_model=list[CurrencyOut])
async def currencies(_: Read, db: DB) -> Any:
    return await svc.list_currencies(get_context(), db)
