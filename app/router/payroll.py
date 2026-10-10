"""Payroll Export (Epic 28, FR-140..FR-142): pay calendars and periods, exceptions, previews,
finalize (★ human only, Idempotency-Key), supplementary versions, files through mapping templates,
Mark Processed, reopen (★), settings and the payroll connector. Internal only (no portal exposure).
Each endpoint calls one service
function.

Permissions: ``payroll:read``, ``payroll:generate`` (previews), ``payroll:finalize``
(finalize, mark processed), ``payroll:configure`` (calendars, mappings, settings, connector),
``period:reopen_pay`` (reopen);
``identity:view`` unmasks PAN / bank in files.
"""

import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.payroll import (
    CatalogueField,
    ExceptionsOut,
    ItemOut,
    MappingIn,
    MappingOut,
    MappingUpdate,
    MarkProcessedIn,
    PayCalendarIn,
    PayCalendarOut,
    PayPeriodOut,
    PayrollConnectorIn,
    PayrollConnectorOut,
    PayrollSettingsOut,
    PayrollSettingsUpdate,
    PreviewIn,
    ReopenIn,
    RowOut,
    VersionOut,
)
from app.services.payroll import connector, exports, files, mappings, pay_periods

router = APIRouter(prefix="/payroll", tags=["payroll"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("payroll:read"))]
Generate = Annotated[Principal, Depends(require("payroll:generate"))]
Finalize = Annotated[Principal, Depends(require("payroll:finalize"))]
Configure = Annotated[Principal, Depends(require("payroll:configure"))]
ReopenPay = Annotated[Principal, Depends(require("period:reopen_pay"))]
Idem = Annotated[IdempotencyGuard, Depends(idempotency())]


# ---------------------------------------------------------------- settings / calendars / connector
@router.get("/settings", response_model=PayrollSettingsOut)
async def get_settings(_: Read, db: DB) -> Any:
    return await pay_periods.get_settings(get_context(), db)


@router.patch("/settings", response_model=PayrollSettingsOut)
async def update_settings(data: PayrollSettingsUpdate, _: Configure, db: DB) -> Any:
    return await pay_periods.update_settings(get_context(), db, data)


@router.get("/calendars/{legal_entity_id}", response_model=PayCalendarOut)
async def get_calendar(legal_entity_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await pay_periods.get_calendar(get_context(), db, legal_entity_id)


@router.put("/calendars/{legal_entity_id}", response_model=PayCalendarOut)
async def put_calendar(legal_entity_id: uuid.UUID, data: PayCalendarIn, _: Configure, db: DB) -> Any:
    return await pay_periods.put_calendar(get_context(), db, legal_entity_id, data)


@router.get("/connector/{legal_entity_id}", response_model=PayrollConnectorOut)
async def get_connector(legal_entity_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await connector.get_connector(get_context(), db, legal_entity_id)


@router.put("/connector/{legal_entity_id}", response_model=PayrollConnectorOut)
async def put_connector(legal_entity_id: uuid.UUID, data: PayrollConnectorIn, _: Configure, db: DB) -> Any:
    return await connector.put_connector(get_context(), db, legal_entity_id, data)


# ---------------------------------------------------------------- periods
@router.get("/periods", response_model=list[PayPeriodOut])
async def list_periods(
    _: Read,
    db: DB,
    legal_entity_id: uuid.UUID | None = None,
    status_: Annotated[Literal["open", "locked"] | None, Query(alias="status")] = None,
) -> Any:
    return await pay_periods.list_periods(get_context(), db, legal_entity_id=legal_entity_id, status=status_)


@router.get("/periods/{period_id}", response_model=PayPeriodOut)
async def get_period(period_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await pay_periods.get_period(get_context(), db, period_id)


@router.get("/periods/{period_id}/exceptions", response_model=ExceptionsOut)
async def period_exceptions(period_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await exports.exceptions(get_context(), db, period_id)


@router.post("/periods/{period_id}/previews", response_model=VersionOut, status_code=status.HTTP_201_CREATED)
async def create_preview(period_id: uuid.UUID, _: Generate, db: DB, data: PreviewIn | None = None) -> Any:
    return await exports.generate_preview(get_context(), db, period_id, data or PreviewIn())


@router.post("/periods/{period_id}/reopen", response_model=PayPeriodOut)
async def reopen_period(period_id: uuid.UUID, data: ReopenIn, _: ReopenPay, db: DB) -> Any:
    return await pay_periods.reopen(get_context(), db, period_id, data.reason)


# ---------------------------------------------------------------- versions
@router.get("/versions", response_model=list[VersionOut])
async def list_versions(_: Read, db: DB, period_id: uuid.UUID | None = None) -> Any:
    return await exports.list_versions(get_context(), db, period_id=period_id)


@router.get("/versions/{version_id}", response_model=VersionOut)
async def get_version(version_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await exports.get_version(get_context(), db, version_id)


@router.get("/versions/{version_id}/rows", response_model=list[RowOut])
async def version_rows(version_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await exports.list_rows(get_context(), db, version_id)


@router.get("/versions/{version_id}/rows/{row_id}/items", response_model=list[ItemOut])
async def row_items(version_id: uuid.UUID, row_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await exports.list_items(get_context(), db, version_id, row_id)


@router.delete("/versions/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preview(version_id: uuid.UUID, _: Generate, db: DB) -> Response:
    await exports.delete_preview(get_context(), db, version_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/versions/{version_id}/finalize", response_model=VersionOut)
async def finalize(version_id: uuid.UUID, _: Finalize, db: DB, idem: Idem) -> Any:
    return await idem.run(lambda: exports.finalize(get_context(), db, version_id))


@router.post("/versions/{version_id}/mark-processed", response_model=VersionOut)
async def mark_processed(
    version_id: uuid.UUID, _: Finalize, db: DB, data: MarkProcessedIn | None = None
) -> Any:
    ref = data.payroll_reference if data else None
    return await exports.mark_processed(get_context(), db, version_id, ref or None)


@router.get("/versions/{version_id}/file", response_class=Response)
async def version_file(
    version_id: uuid.UUID,
    _: Read,
    db: DB,
    mapping_id: uuid.UUID | None = None,
    format_: Annotated[Literal["csv", "xlsx"] | None, Query(alias="format")] = None,
) -> Response:
    result = await files.build_file(get_context(), db, version_id, mapping_id, format_)
    return Response(
        content=result.data,
        media_type=result.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(result.filename)}",
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------- mappings
@router.get("/mappings", response_model=list[MappingOut])
async def list_mappings(_: Read, db: DB) -> Any:
    return await mappings.list_mappings(get_context(), db)


@router.get("/mappings/catalogue", response_model=list[CatalogueField])
async def mapping_catalogue(_: Read) -> Any:
    return mappings.catalogue()


@router.post("/mappings", response_model=MappingOut, status_code=status.HTTP_201_CREATED)
async def create_mapping(data: MappingIn, _: Configure, db: DB) -> Any:
    return await mappings.create_mapping(get_context(), db, data)


@router.patch("/mappings/{mapping_id}", response_model=MappingOut)
async def update_mapping(mapping_id: uuid.UUID, data: MappingUpdate, _: Configure, db: DB) -> Any:
    return await mappings.update_mapping(get_context(), db, mapping_id, data)
