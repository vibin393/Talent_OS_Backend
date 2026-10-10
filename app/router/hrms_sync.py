# ruff: noqa: E501
"""HRMS sync API (Epic 71, spec A11), prefix ``/hrms``.

| Method + path | Permission |
|---|---|
| ``GET|PUT /hrms/settings`` | ``hrms:sync`` |
| ``GET|PUT /hrms/installations/{id}/entities/{le_id}/mappings`` | ``hrms:sync`` |
| ``POST /hrms/mappings/{id}/enable-sensitive`` (human; + ``ledger:sync``) · ``POST /hrms/mappings/{id}/disable-sensitive`` | ``hrms:sync`` |
| ``GET /hrms/records`` | ``hrms:sync`` |
| ``GET /hrms/placements/{placement_id}`` | ``placement:read`` |
| ``POST /hrms/records/{id}/retry|skip`` · ``POST /hrms/placements/{placement_id}/include`` | ``hrms:sync`` |
| ``POST /hrms/installations/{id}/entities/{le_id}/import-file`` | ``hrms:sync`` |
| ``POST /hrms/installations/{id}/pull`` | ``hrms:sync`` |

Responses carry ids, statuses and sanitised messages -- never field values.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.pagination import Page
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.hrms_sync import (
    HrmsRecordOut,
    HrmsSettingsIn,
    HrmsSettingsOut,
    IncludeIn,
    JobQueuedOut,
    MappingOut,
    MappingPageOut,
    MappingsIn,
    PlacementPanelOut,
    SensitiveIn,
    SkipIn,
)
from app.services import hrms_sync_service as svc

router = APIRouter(prefix="/hrms", tags=["hrms-sync"])
DB = TenantDBSessionDep
Sync = Annotated[Principal, Depends(require("hrms:sync"))]
PlacementRead = Annotated[Principal, Depends(require("placement:read"))]


@router.get("/settings", response_model=HrmsSettingsOut)
async def get_settings(_: Sync, db: DB) -> Any:
    return await svc.get_settings(get_context(), db)


@router.put("/settings", response_model=HrmsSettingsOut)
async def put_settings(body: HrmsSettingsIn, _: Sync, db: DB) -> Any:
    return await svc.put_settings(get_context(), db, body)


@router.get("/installations/{installation_id}/entities/{legal_entity_id}/mappings", response_model=MappingPageOut)
async def get_mappings(installation_id: uuid.UUID, legal_entity_id: uuid.UUID, _: Sync, db: DB) -> Any:
    return await svc.get_mappings(get_context(), db, installation_id, legal_entity_id)


@router.put("/installations/{installation_id}/entities/{legal_entity_id}/mappings", response_model=MappingPageOut)
async def put_mappings(installation_id: uuid.UUID, legal_entity_id: uuid.UUID, body: MappingsIn, _: Sync, db: DB) -> Any:
    return await svc.put_mappings(get_context(), db, installation_id, legal_entity_id, body.items)


@router.post("/mappings/{mapping_id}/enable-sensitive", response_model=MappingOut)
async def enable_sensitive(mapping_id: uuid.UUID, body: SensitiveIn, _: Sync, db: DB) -> Any:
    ctx = get_context()
    require_human(actor_from_ctx(ctx))
    return await svc.enable_sensitive(ctx, db, mapping_id, body.purpose)


@router.post("/mappings/{mapping_id}/disable-sensitive", response_model=MappingOut)
async def disable_sensitive(mapping_id: uuid.UUID, _: Sync, db: DB) -> Any:
    return await svc.disable_sensitive(get_context(), db, mapping_id)


@router.get("/records", response_model=Page[HrmsRecordOut])
async def list_records(
    _: Sync,
    db: DB,
    record_status: Annotated[Literal["pending", "blocked", "synced", "failed", "skipped"] | None, Query(alias="status")] = None,
    legal_entity_id: uuid.UUID | None = None,
    mismatch: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    return await svc.list_records(get_context(), db, status=record_status, legal_entity_id=legal_entity_id, mismatch=mismatch, page=page, page_size=page_size)


@router.get("/placements/{placement_id}", response_model=PlacementPanelOut)
async def placement_panel(placement_id: uuid.UUID, _: PlacementRead, db: DB) -> Any:
    return await svc.placement_panel(get_context(), db, placement_id)


@router.post("/records/{record_id}/retry", response_model=JobQueuedOut)
async def retry(record_id: uuid.UUID, _: Sync, db: DB) -> Any:
    return await svc.retry(get_context(), db, record_id)


@router.post("/records/{record_id}/skip", response_model=HrmsRecordOut)
async def skip(record_id: uuid.UUID, body: SkipIn, _: Sync, db: DB) -> Any:
    return await svc.skip(get_context(), db, record_id, body.reason)


@router.post("/placements/{placement_id}/include", response_model=PlacementPanelOut)
async def include(placement_id: uuid.UUID, body: IncludeIn, _: Sync, db: DB) -> Any:
    return await svc.include(get_context(), db, placement_id, body.include)


@router.post("/installations/{installation_id}/entities/{legal_entity_id}/import-file")
async def import_file(installation_id: uuid.UUID, legal_entity_id: uuid.UUID, _: Sync, db: DB) -> Response:
    dl = await svc.build_import_file(get_context(), db, installation_id, legal_entity_id)
    return Response(content=dl.data, media_type=dl.content_type, headers={"Content-Disposition": f'attachment; filename="{dl.filename}"'})


@router.post("/installations/{installation_id}/pull", response_model=JobQueuedOut)
async def pull_now(installation_id: uuid.UUID, _: Sync, db: DB) -> Any:
    return await svc.pull_now(get_context(), db, installation_id)
