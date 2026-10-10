# ruff: noqa: E501
"""26AS / AIS TDS reconciliation (Epic 51, FR-254): ``/receivables/tds-recon/*``.

Reads need ``receivables:read``; runs, decisions and TAN mappings ``tds_recon:write``. Match decisions are
human-only (FR-257): API keys / workflow get 403 ``human_only_action``. Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.tds_recon import (
    TdsAcknowledgeIn,
    TdsCandidateOut,
    TdsDeductorTanIn,
    TdsDeductorTanOut,
    TdsExceptionsOut,
    TdsManualMatchIn,
    TdsMatchDecisionIn,
    TdsMatchOut,
    TdsReconRunDetailOut,
    TdsReconRunOut,
    TdsStatusRowOut,
)
from app.services import files_service
from app.services.receivables.tds_recon import exceptions, runs
from app.services.receivables.tds_recon import status as recon_status

router = APIRouter(prefix="/receivables/tds-recon", tags=["tds-reconciliation"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("receivables:read"))]
Write = Annotated[Principal, Depends(require("tds_recon:write"))]
Quarter = Literal["Q1", "Q2", "Q3", "Q4"]
FyQuery = Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")]


@router.get("/runs", response_model=list[TdsReconRunOut])
async def list_runs(
    _: Read,
    db: DB,
    legal_entity_id: uuid.UUID | None = None,
    fy: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    quarter: Quarter | None = None,
) -> Any:
    return await runs.list_runs(get_context(), db, legal_entity_id=legal_entity_id, fy=fy, quarter=quarter)


@router.post("/runs", response_model=TdsReconRunOut, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    _: Write,
    db: DB,
    legal_entity_id: Annotated[uuid.UUID, Form()],
    fy: Annotated[str, Form(pattern=r"^\d{4}-\d{2}$")],
    quarter: Annotated[Quarter, Form()],
    statement_kind: Annotated[Literal["26as", "ais"], Form()] = "26as",
    file: Annotated[UploadFile | None, File()] = None,
) -> Any:
    data = await files_service.read_upload(file) if file is not None else None
    return await runs.create_run(
        get_context(),
        db,
        legal_entity_id=legal_entity_id,
        fy=fy,
        quarter=quarter,
        statement_kind=statement_kind,
        data=data or None,
        filename=file.filename if file is not None else None,
    )


@router.get("/runs/{run_id}", response_model=TdsReconRunDetailOut)
async def get_run(run_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await runs.run_detail(get_context(), db, run_id)


@router.get("/lines/{line_id}/candidates", response_model=list[TdsCandidateOut])
async def line_candidates(line_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await runs.candidates(get_context(), db, line_id)


@router.post("/matches/{match_id}/confirm", response_model=TdsMatchOut)
async def confirm_match(match_id: uuid.UUID, _: Write, db: DB, body: TdsMatchDecisionIn | None = None) -> Any:
    return await runs.confirm(get_context(), db, match_id, body.note if body else None)


@router.post("/matches/{match_id}/reject", response_model=TdsMatchOut)
async def reject_match(match_id: uuid.UUID, _: Write, db: DB, body: TdsMatchDecisionIn | None = None) -> Any:
    return await runs.reject(get_context(), db, match_id, body.note if body else None)


@router.post("/lines/{line_id}/match", response_model=TdsMatchOut)
async def pair_line(line_id: uuid.UUID, body: TdsManualMatchIn, _: Write, db: DB) -> Any:
    return await runs.pair(
        get_context(), db, line_id, body.tds_deduction_id, record_tan_mapping=body.record_tan_mapping
    )


@router.post("/lines/{line_id}/acknowledge", response_model=TdsMatchOut)
async def acknowledge_line(line_id: uuid.UUID, body: TdsAcknowledgeIn, _: Write, db: DB) -> Any:
    return await runs.acknowledge(get_context(), db, line_id, body.note)


@router.get("/exceptions", response_model=TdsExceptionsOut)
async def get_exceptions(_: Read, db: DB, legal_entity_id: uuid.UUID, fy: FyQuery, quarter: Quarter) -> Any:
    return await exceptions.exceptions(get_context(), db, legal_entity_id, fy, quarter)


@router.get("/status", response_model=list[TdsStatusRowOut])
async def get_status(_: Read, db: DB, fy: FyQuery, legal_entity_id: uuid.UUID | None = None) -> Any:
    return await recon_status.status(get_context(), db, fy, legal_entity_id)


@router.get("/deductor-tans", response_model=list[TdsDeductorTanOut])
async def list_tans(_: Read, db: DB) -> Any:
    return await runs.list_tans(get_context(), db)


@router.post("/deductor-tans", response_model=TdsDeductorTanOut, status_code=status.HTTP_201_CREATED)
async def create_tan(body: TdsDeductorTanIn, _: Write, db: DB) -> Any:
    return await runs.create_tan(get_context(), db, body)


@router.delete("/deductor-tans/{tan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tan(tan_id: uuid.UUID, _: Write, db: DB) -> Response:
    await runs.delete_tan(get_context(), db, tan_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
