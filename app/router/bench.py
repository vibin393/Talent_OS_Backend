"""Bench management (Epic 24, FR-125..FR-127), prefix ``/api/v1/bench``.

Permissions: ``bench:read`` (list, detail, matches, summary, roll-offs), ``bench:write`` (add, edit, release,
settings), ``bench:edit_cost`` (cost override), ``bench:review_matches`` (review / dismiss / complete). Bench
matches additionally go through the P1 demand scope (404 outside it). Cost is masked without
``compensation:view``. Each endpoint calls one service function.
"""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.bench import (
    BenchEntryCreate,
    BenchEntryOut,
    BenchEntryPage,
    BenchEntryUpdate,
    BenchMatchesOut,
    BenchSettingsOut,
    BenchSettingsUpdate,
    BenchSummaryOut,
    CostOverrideIn,
    ReleaseIn,
    ReviewCompleteOut,
    ReviewIn,
    ReviewOut,
    RollOffRow,
)
from app.services.placements import bench, bench_matching, bench_reporting

router = APIRouter(prefix="/bench", tags=["bench"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("bench:read"))]
Write = Annotated[Principal, Depends(require("bench:write"))]
EditCost = Annotated[Principal, Depends(require("bench:edit_cost"))]
Review = Annotated[Principal, Depends(require("bench:review_matches"))]


@router.get("/entries", response_model=BenchEntryPage)
async def list_entries(
    _: Read,
    db: DB,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    skill_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    location: str | None = None,
    available_before: date | None = None,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    q: str | None = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> Any:
    return await bench.list_entries(
        get_context(),
        db,
        status=status_ or (),
        skill_ids=skill_id or (),
        location=location,
        available_before=available_before,
        legal_entity_id=legal_entity_id,
        client_id=client_id,
        q=q,
        page=page,
        page_size=page_size,
    )


@router.post("/entries", response_model=BenchEntryOut, status_code=status.HTTP_201_CREATED)
async def create_entry(data: BenchEntryCreate, _: Write, db: DB) -> Any:
    return await bench.create_entry(get_context(), db, data)


@router.get("/entries/{entry_id}", response_model=BenchEntryOut)
async def get_entry(entry_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await bench.get_entry(get_context(), db, entry_id)


@router.patch("/entries/{entry_id}", response_model=BenchEntryOut)
async def update_entry(entry_id: uuid.UUID, data: BenchEntryUpdate, _: Write, db: DB) -> Any:
    return await bench.update_entry(get_context(), db, entry_id, data)


@router.post("/entries/{entry_id}/cost", response_model=BenchEntryOut)
async def override_cost(entry_id: uuid.UUID, data: CostOverrideIn, _: EditCost, db: DB) -> Any:
    return await bench.override_cost(get_context(), db, entry_id, data)


@router.post("/entries/{entry_id}/release", response_model=BenchEntryOut)
async def release_entry(entry_id: uuid.UUID, data: ReleaseIn, _: Write, db: DB) -> Any:
    return await bench.release(get_context(), db, entry_id, data)


@router.get("/settings", response_model=BenchSettingsOut)
async def get_settings(_: Write, db: DB) -> Any:
    return await bench.get_settings(get_context(), db)


@router.patch("/settings", response_model=BenchSettingsOut)
async def update_settings(data: BenchSettingsUpdate, _: Write, db: DB) -> Any:
    return await bench.update_settings(get_context(), db, data)


@router.get("/demands/{demand_id}/matches", response_model=BenchMatchesOut)
async def demand_matches(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await bench_matching.bench_matches(get_context(), db, demand_id)


@router.post("/demands/{demand_id}/reviews", response_model=ReviewOut)
async def review_match(demand_id: uuid.UUID, data: ReviewIn, _: Review, db: DB) -> Any:
    return await bench_matching.review(get_context(), db, demand_id, data)


@router.post("/demands/{demand_id}/reviews/complete", response_model=ReviewCompleteOut)
async def complete_review(demand_id: uuid.UUID, _: Review, db: DB) -> Any:
    return await bench_matching.complete_review(get_context(), db, demand_id)


@router.get("/summary", response_model=BenchSummaryOut)
async def summary(
    _: Read,
    db: DB,
    as_of: date | None = None,
    months: Annotated[int, Query(ge=1, le=24)] = 6,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
) -> Any:
    return await bench_reporting.summary(
        get_context(), db, as_of, months=months, legal_entity_id=legal_entity_id, client_id=client_id
    )


@router.get("/roll-offs", response_model=list[RollOffRow])
async def roll_offs(_: Read, db: DB, within: Annotated[int, Query(ge=1, le=365)] = 30) -> Any:
    return await bench_reporting.roll_offs(get_context(), db, within)
