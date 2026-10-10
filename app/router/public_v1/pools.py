# ruff: noqa: E501
"""``/public/v1/pools`` (Epic 67, spec A4): talent pools the caller can see and their members (candidate ids)."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import PoolMemberOut, PoolOut
from app.services import public_api_service as svc

router = public_router("pools")
Read = scope("pools:read")


@router.get("/pools", response_model=Page[PoolOut])
async def list_pools(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.directory.list_pools(db, **page.kw())


@router.get("/pools/{pool_id}", response_model=PoolOut)
async def get_pool(pool_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.directory.get_pool(db, pool_id)


@router.get("/pools/{pool_id}/members", response_model=Page[PoolMemberOut])
async def pool_members(pool_id: uuid.UUID, _: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.directory.pool_members(db, pool_id, **page.kw())
