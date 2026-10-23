# ruff: file-ignore[line-too-long]
"""Ledger sync API (Epic 71, spec A11), prefix ``/ledger``.

| Method + path | Permission |
|---|---|
| ``GET|PUT /ledger/settings`` | ``ledger:sync`` |
| ``GET|PUT /ledger/installations/{id}/entities/{le_id}/mappings`` · ``POST .../load-masters`` | ``ledger:sync`` |
| ``GET /ledger/records`` | ``ledger:sync`` |
| ``GET /ledger/documents/{kind}/{id}`` | ``billing:read`` |
| ``POST /ledger/records/{id}/retry|skip`` | ``ledger:sync`` |
| ``POST /ledger/runs`` (+ Idempotency-Key) · ``GET /ledger/runs`` · ``GET /ledger/runs/{id}/file`` | ``ledger:sync`` |
| ``GET /ledger/payment-suggestions`` · ``POST .../{id}/confirm`` (★ human; + ``payment:record``) · ``POST .../{id}/reject`` (human) | ``ledger:sync`` |
| ``GET /ledger/reconciliation`` · ``POST /ledger/reconciliation/export`` | ``ledger:sync`` |
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Response

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.pagination import Page
from app.core.permissions import Principal, forbidden, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.ledger_sync import (
    DocumentPanelOut,
    LedgerMappingPageOut,
    LedgerMappingsIn,
    LedgerRecordOut,
    LedgerRunOut,
    LedgerSettingsIn,
    LedgerSettingsOut,
    MastersOut,
    PaymentSuggestionOut,
    ReconciliationOut,
    RejectIn,
    RunIn,
    SkipIn,
)
from app.schemas.receivables import PaymentIn
from app.services import ledger_sync_service as svc

router = APIRouter(prefix="/ledger", tags=["ledger-sync"])
DB = TenantDBSessionDep
Sync = Annotated[Principal, Depends(require("ledger:sync"))]
BillingRead = Annotated[Principal, Depends(require("billing:read"))]
Flag = Literal["amount_differs", "status_differs", "only_in_talent_os", "only_in_ledger"]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/settings", response_model=LedgerSettingsOut)
async def get_settings(_: Sync, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.put("/settings", response_model=LedgerSettingsOut)
async def put_settings(body: LedgerSettingsIn, _: Sync, db: DB) -> Any:
    return await svc.put_settings(get_context(), db, body)


@router.get("/installations/{installation_id}/entities/{legal_entity_id}/mappings", response_model=LedgerMappingPageOut)
async def get_mappings(installation_id: uuid.UUID, legal_entity_id: uuid.UUID, _: Sync, db: DB) -> Any:
    return await svc.get_mappings(get_context(), db, installation_id, legal_entity_id)


@router.put("/installations/{installation_id}/entities/{legal_entity_id}/mappings", response_model=LedgerMappingPageOut)
async def put_mappings(installation_id: uuid.UUID, legal_entity_id: uuid.UUID, body: LedgerMappingsIn, _: Sync, db: DB) -> Any:
    return await svc.put_mappings(get_context(), db, installation_id, legal_entity_id, body.items)


@router.post("/installations/{installation_id}/entities/{legal_entity_id}/load-masters", response_model=MastersOut)
async def load_masters(installation_id: uuid.UUID, legal_entity_id: uuid.UUID, _: Sync, db: DB) -> Any:
    return await svc.load_masters(get_context(), db, installation_id, legal_entity_id)


@router.get("/records", response_model=Page[LedgerRecordOut])
async def list_records(
    _: Sync,
    db: DB,
    record_status: Annotated[Literal["pending", "synced", "failed", "skipped", "out_of_sync", "blocked_period"] | None, Query(alias="status")] = None,
    kind: Literal["invoice", "credit_note", "debit_note", "payment"] | None = None,
    legal_entity_id: uuid.UUID | None = None,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    return await svc.list_records(get_context(), db, status=record_status, kind=kind, legal_entity_id=legal_entity_id, from_date=from_date, to_date=to_date, page=page, page_size=page_size)


@router.get("/documents/{kind}/{document_id}", response_model=DocumentPanelOut)
async def document_panel(kind: Literal["invoice", "credit_note", "debit_note", "payment"], document_id: uuid.UUID, _: BillingRead, db: DB) -> Any:
    return await svc.document_panel(get_context(), db, kind, document_id)


@router.post("/records/{record_id}/retry", response_model=LedgerRecordOut)
async def retry(record_id: uuid.UUID, _: Sync, db: DB) -> Any:
    return await svc.retry(get_context(), db, record_id)


@router.post("/records/{record_id}/skip", response_model=LedgerRecordOut)
async def skip(record_id: uuid.UUID, body: SkipIn, _: Sync, db: DB) -> Any:
    return await svc.skip(get_context(), db, record_id, body.reason)


@router.post("/runs", response_model=LedgerRunOut, status_code=201)
async def create_run(body: RunIn, _: Sync, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]) -> Any:
    ctx = get_context()

    async def _run() -> LedgerRunOut:
        return await svc.create_run(ctx, db, body)

    return await idem.run(_run)


@router.get("/runs", response_model=Page[LedgerRunOut])
async def list_runs(
    _: Sync,
    db: DB,
    legal_entity_id: uuid.UUID | None = None,
    kind: Literal["push", "tally_file", "pull_payments", "status", "masters"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    return await svc.list_runs(get_context(), db, legal_entity_id=legal_entity_id, kind=kind, page=page, page_size=page_size)


@router.get("/runs/{run_id}/file")
async def run_file(run_id: uuid.UUID, _: Sync, db: DB) -> Response:
    dl = await svc.run_file(get_context(), db, run_id)
    return Response(content=dl.data, media_type=dl.content_type, headers={"Content-Disposition": f'attachment; filename="{dl.filename}"'})


@router.get("/payment-suggestions", response_model=Page[PaymentSuggestionOut])
async def list_suggestions(
    _: Sync,
    db: DB,
    suggestion_status: Annotated[Literal["pending", "confirmed", "rejected", "stale"] | None, Query(alias="status")] = None,
    legal_entity_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    return await svc.list_suggestions(get_context(), db, status=suggestion_status, legal_entity_id=legal_entity_id, page=page, page_size=page_size)


@router.post("/payment-suggestions/{suggestion_id}/confirm", response_model=PaymentSuggestionOut)
async def confirm(suggestion_id: uuid.UUID, principal: Sync, db: DB, body: Annotated[PaymentIn | None, Body()] = None) -> Any:
    _human()
    if not principal.has("payment:record"):
        raise forbidden("payment:record")
    return await svc.confirm(get_context(), db, suggestion_id, body)


@router.post("/payment-suggestions/{suggestion_id}/reject", response_model=PaymentSuggestionOut)
async def reject(suggestion_id: uuid.UUID, body: RejectIn, _: Sync, db: DB) -> Any:
    _human()
    return await svc.reject(get_context(), db, suggestion_id, body.reason)


@router.get("/reconciliation", response_model=ReconciliationOut)
async def reconciliation(
    _: Sync,
    db: DB,
    legal_entity_id: uuid.UUID,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    flag: Flag | None = None,
) -> Any:
    return await svc.reconcile(get_context(), db, legal_entity_id, from_date, to_date, flag)


@router.post("/reconciliation/export")
async def reconciliation_export(
    _: Sync,
    db: DB,
    legal_entity_id: uuid.UUID,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    flag: Flag | None = None,
) -> Response:
    data = await svc.export_csv(get_context(), db, legal_entity_id, from_date, to_date, flag)
    return Response(content=data, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="ledger-reconciliation.csv"'})
