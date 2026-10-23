# ruff: file-ignore[line-too-long]
"""Talent pools & rediscovery (Epic 38, FR-192..FR-195): ``/api/v1/talent-pools`` and the Demand pins /
add-to-pipeline endpoints.

Permissions: ``pool:read`` (visible Pools), ``pool:write`` (own Pools, members, pins), ``pool:manage`` (tenant
Pools, templates, settings, any Pool). Members are always re-filtered by the viewer's ``candidate:read`` scope.
The rediscovery summary itself is ``GET /demands/{id}/rediscovery`` (``router/matching.py``).
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.pipeline import ApplicationsCreateOut
from app.schemas.talent_pools import (
    AddToPipelineIn,
    PoolCreate,
    PoolMembersAddOut,
    PoolMembersIn,
    PoolMembersPage,
    PoolOut,
    PoolPinIn,
    PoolPinOut,
    PoolSettingsIn,
    PoolSettingsOut,
    PoolUpdate,
    RefreshOut,
    SubscriptionOut,
    TemplateEnableIn,
    TemplateOut,
)
from app.services.talent_pools import api

router = APIRouter(tags=["talent-pools"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("pool:read"))]
Write = Annotated[Principal, Depends(require("pool:write"))]
Manage = Annotated[Principal, Depends(require("pool:manage"))]
CreateApp = Annotated[Principal, Depends(require("application:create"))]

P = "/talent-pools"


@router.get(P, response_model=list[PoolOut])
async def list_pools(
    _: Read,
    db: DB,
    filter: Annotated[Literal["all", "mine", "team", "tenant"], Query()] = "all",
    kind: Annotated[Literal["static", "dynamic"] | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    writable: Annotated[bool, Query()] = False,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[PoolOut]:
    return await api.list_pools(
        get_context(),
        db,
        filter_=filter,
        kind=kind,
        include_archived=include_archived,
        q=q,
        writable=writable,
    )


@router.post(P, response_model=PoolOut, status_code=status.HTTP_201_CREATED)
async def create_pool(body: PoolCreate, _: Write, db: DB) -> PoolOut:
    return await api.create_pool(get_context(), db, body)


@router.get(f"{P}/templates", response_model=list[TemplateOut])
async def list_templates(_: Manage, db: DB) -> list[TemplateOut]:
    return await api.list_templates(get_context(), db)


@router.post(f"{P}/templates/{{key}}/enable", response_model=PoolOut, status_code=status.HTTP_201_CREATED)
async def enable_template(key: str, _: Manage, db: DB, body: TemplateEnableIn | None = None) -> PoolOut:
    return await api.enable_template(get_context(), db, key, body.batch if body else None)


@router.get(f"{P}/settings", response_model=PoolSettingsOut)
async def get_settings(_: Manage, db: DB) -> PoolSettingsOut:
    return await api.read_settings(get_context(), db)


@router.put(f"{P}/settings", response_model=PoolSettingsOut)
async def put_settings(body: PoolSettingsIn, _: Manage, db: DB) -> PoolSettingsOut:
    return await api.update_settings(get_context(), db, body)


@router.get(f"{P}/{{pool_id}}", response_model=PoolOut)
async def get_pool(pool_id: uuid.UUID, _: Read, db: DB) -> PoolOut:
    return await api.get_pool(get_context(), db, pool_id)


@router.patch(f"{P}/{{pool_id}}", response_model=PoolOut)
async def update_pool(pool_id: uuid.UUID, body: PoolUpdate, _: Write, db: DB) -> PoolOut:
    return await api.update_pool(get_context(), db, pool_id, body)


@router.delete(f"{P}/{{pool_id}}", response_model=PoolOut)
async def archive_pool(pool_id: uuid.UUID, _: Write, db: DB) -> PoolOut:
    return await api.archive_pool(get_context(), db, pool_id)


@router.get(f"{P}/{{pool_id}}/members", response_model=PoolMembersPage)
async def list_members(
    pool_id: uuid.UUID,
    _: Read,
    db: DB,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PoolMembersPage:
    return await api.list_members(get_context(), db, pool_id, page=page, page_size=page_size)


@router.post(f"{P}/{{pool_id}}/members", response_model=PoolMembersAddOut)
async def add_members(pool_id: uuid.UUID, body: PoolMembersIn, _: Write, db: DB) -> PoolMembersAddOut:
    return await api.add_members(get_context(), db, pool_id, body)


@router.delete(f"{P}/{{pool_id}}/members/{{candidate_id}}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(pool_id: uuid.UUID, candidate_id: uuid.UUID, _: Write, db: DB) -> Response:
    await api.remove_member(get_context(), db, pool_id, candidate_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(f"{P}/{{pool_id}}/refresh", response_model=RefreshOut, status_code=status.HTTP_202_ACCEPTED)
async def refresh_pool(pool_id: uuid.UUID, _: Read, db: DB) -> RefreshOut:
    return await api.refresh_pool(get_context(), db, pool_id)


@router.get(f"{P}/{{pool_id}}/refreshes", response_model=list[RefreshOut])
async def list_refreshes(pool_id: uuid.UUID, _: Read, db: DB) -> list[RefreshOut]:
    return await api.list_refreshes(get_context(), db, pool_id)


@router.post(f"{P}/{{pool_id}}/subscription", response_model=SubscriptionOut)
async def subscribe(pool_id: uuid.UUID, _: Read, db: DB) -> SubscriptionOut:
    return await api.set_subscription(get_context(), db, pool_id, True)


@router.delete(f"{P}/{{pool_id}}/subscription", response_model=SubscriptionOut)
async def unsubscribe(pool_id: uuid.UUID, _: Read, db: DB) -> SubscriptionOut:
    return await api.set_subscription(get_context(), db, pool_id, False)


# ---------------------------------------------------------------- demand pins / add to pipeline
@router.get("/demands/{demand_id}/pool-pins", response_model=list[PoolPinOut])
async def list_pins(demand_id: uuid.UUID, _: Read, db: DB) -> list[PoolPinOut]:
    return await api.list_pins(get_context(), db, demand_id)


@router.post("/demands/{demand_id}/pool-pins", response_model=PoolPinOut, status_code=status.HTTP_201_CREATED)
async def pin_pool(demand_id: uuid.UUID, body: PoolPinIn, _: Write, db: DB) -> PoolPinOut:
    return await api.pin(get_context(), db, demand_id, body.pool_id)


@router.delete("/demands/{demand_id}/pool-pins/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unpin_pool(demand_id: uuid.UUID, pool_id: uuid.UUID, _: Write, db: DB) -> Response:
    await api.unpin(get_context(), db, demand_id, pool_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/demands/{demand_id}/pools/{pool_id}/add-to-pipeline", response_model=ApplicationsCreateOut)
async def add_to_pipeline(
    demand_id: uuid.UUID, pool_id: uuid.UUID, body: AddToPipelineIn, _: CreateApp, __: Read, db: DB
) -> Any:
    return await api.add_to_pipeline(get_context(), db, demand_id, pool_id, body)
