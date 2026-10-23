# ruff: file-ignore[line-too-long]
"""Bias audit (Epic 49, FR-246..FR-249): ``/api/v1/fairness/*``. Internal Users only.

Aggregate-only by construction: no endpoint here returns an individual's self-ID answers (an OpenAPI scan in
``tests/modules/fairness`` proves it). ``fairness:read`` -- runs, reports, packs, inventory and publication lists;
``fairness:configure`` -- settings, category labels, LL144 flags, publishing; ``aedt:manage`` -- the AEDT inventory.
Each endpoint calls one service function.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

import app.models.fairness  # ruff: ignore[unused-import]  - fairness tables on Base.metadata at app start (AD-47)  # noqa: F401
from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.fairness import (
    AedtIn,
    AedtOut,
    AedtPatch,
    AlternativeRequestOut,
    CategorySetIn,
    FairnessSettingsOut,
    FairnessSettingsPatch,
    Ll144FlagIn,
    Ll144FlagOut,
    PackIn,
    PackOut,
    PublicationIn,
    PublicationOut,
    RunCreate,
    RunOut,
    RunSummaryOut,
    SelectionPointOut,
)
from app.services.fairness import (
    alternatives,
    inventory,
    ll144,
    pack,
    publications,
    reports,
    selection_points,
    settings,
)

router = APIRouter(prefix="/fairness", tags=["fairness"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("fairness:read"))]
ReadOrConfigure = Annotated[Principal, Depends(require_any("fairness:read", "fairness:configure"))]
Configure = Annotated[Principal, Depends(require("fairness:configure"))]
InventoryRead = Annotated[Principal, Depends(require_any("fairness:read", "aedt:manage"))]
Manage = Annotated[Principal, Depends(require("aedt:manage"))]
AppRead = Annotated[Principal, Depends(require("application:read"))]
FileName = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,80}$")]


@router.get("/settings", response_model=FairnessSettingsOut)
async def get_settings(_: ReadOrConfigure, db: DB) -> FairnessSettingsOut:
    return await settings.get(get_context(), db)


@router.patch("/settings", response_model=FairnessSettingsOut)
async def patch_settings(_: Configure, db: DB, body: FairnessSettingsPatch) -> FairnessSettingsOut:
    return await settings.patch(get_context(), db, body)


@router.put("/category-set", response_model=FairnessSettingsOut)
async def put_category_set(_: Configure, db: DB, body: CategorySetIn) -> FairnessSettingsOut:
    return await settings.update_category_set(get_context(), db, body)


@router.get("/selection-points", response_model=list[SelectionPointOut])
async def list_selection_points(_: Read, db: DB) -> list[SelectionPointOut]:
    """Epic 65: selection points of the run-request form (Phase 4 points hidden while their tables are absent)."""
    return [SelectionPointOut(**p) for p in await selection_points.catalogue(db)]


@router.get("/runs", response_model=list[RunSummaryOut])
async def list_runs(_: Read, db: DB) -> list[RunSummaryOut]:
    return await reports.list_runs(get_context(), db)


@router.post("/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def create_run(_: Read, db: DB, body: RunCreate) -> RunOut:
    return await reports.create_run(get_context(), db, body)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(_: Read, db: DB, run_id: uuid.UUID) -> RunOut:
    return await reports.get_run(get_context(), db, run_id)


@router.post("/runs/{run_id}/pack", response_model=PackOut)
async def generate_pack(_: Read, db: DB, run_id: uuid.UUID, body: PackIn) -> PackOut:
    return await pack.generate(get_context(), db, run_id, body)


@router.get("/runs/{run_id}/pack/files/{name}", response_class=Response)
async def download_pack_file(_: Read, db: DB, run_id: uuid.UUID, name: FileName) -> Response:
    data, content_type, filename = await pack.download(get_context(), db, run_id, name)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/aedt", response_model=list[AedtOut])
async def list_aedt(_: InventoryRead, db: DB) -> list[AedtOut]:
    return await inventory.list_entries(get_context(), db)


@router.post("/aedt", response_model=AedtOut, status_code=status.HTTP_201_CREATED)
async def create_aedt(_: Manage, db: DB, body: AedtIn) -> AedtOut:
    return await inventory.create(get_context(), db, body)


@router.patch("/aedt/{aedt_id}", response_model=AedtOut)
async def update_aedt(_: Manage, db: DB, aedt_id: uuid.UUID, body: AedtPatch) -> AedtOut:
    return await inventory.update(get_context(), db, aedt_id, body)


@router.get("/publications", response_model=list[PublicationOut])
async def list_publications(_: Read, db: DB) -> list[PublicationOut]:
    return await publications.list_publications(get_context(), db)


@router.post("/publications", response_model=PublicationOut, status_code=status.HTTP_201_CREATED)
async def publish(_: Configure, db: DB, body: PublicationIn) -> PublicationOut:
    return await publications.publish(get_context(), db, body)


@router.post("/publications/{publication_id}/unpublish", response_model=PublicationOut)
async def unpublish(_: Configure, db: DB, publication_id: uuid.UUID) -> PublicationOut:
    return await publications.unpublish(get_context(), db, publication_id)


@router.get("/demands/{demand_id}/ll144", response_model=Ll144FlagOut)
async def get_ll144(_: ReadOrConfigure, db: DB, demand_id: uuid.UUID) -> Ll144FlagOut:
    return await ll144.get_flag(get_context(), db, demand_id)


@router.put("/demands/{demand_id}/ll144", response_model=Ll144FlagOut)
async def put_ll144(_: Configure, db: DB, demand_id: uuid.UUID, body: Ll144FlagIn) -> Ll144FlagOut:
    return await ll144.set_flag(get_context(), db, demand_id, body.flagged)


@router.get("/applications/{application_id}/alternative-request", response_model=AlternativeRequestOut | None)
async def get_alternative_request(
    _: AppRead, db: DB, application_id: uuid.UUID
) -> AlternativeRequestOut | None:
    return await alternatives.for_application(get_context(), db, application_id)


@router.post(
    "/applications/{application_id}/alternative-request/handled", response_model=AlternativeRequestOut
)
async def mark_alternative_handled(_: AppRead, db: DB, application_id: uuid.UUID) -> AlternativeRequestOut:
    return await alternatives.mark_handled(get_context(), db, application_id)
