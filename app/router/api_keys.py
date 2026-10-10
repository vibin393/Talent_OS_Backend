"""Tenant API keys (Epic 12 story 12.7, FR-75): ``api_key:manage``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyRotate, ApiKeyUsageOut
from app.services import api_keys_service

router = APIRouter(prefix="/api-keys", tags=["api-keys"])
KeyManage = Annotated[Principal, Depends(require("api_key:manage"))]


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(_: KeyManage, db: TenantDBSessionDep) -> list[ApiKeyOut]:
    return await api_keys_service.list_keys(get_context(), db)


@router.get("/delegable-scopes", response_model=list[str])
async def delegable_scopes(principal: KeyManage) -> list[str]:
    return api_keys_service.delegable_scopes(dict(principal.permissions))


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(body: ApiKeyCreate, _: KeyManage, db: TenantDBSessionDep) -> ApiKeyCreated:
    return await api_keys_service.create_key(get_context(), db, body)


@router.post("/{key_id}/rotate", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def rotate_key(key_id: uuid.UUID, body: ApiKeyRotate, _: KeyManage, db: TenantDBSessionDep) -> ApiKeyCreated:
    return await api_keys_service.rotate_key(get_context(), db, key_id, body.grace_hours)


@router.post("/{key_id}/revoke", response_model=ApiKeyOut)
async def revoke_key(key_id: uuid.UUID, _: KeyManage, db: TenantDBSessionDep) -> ApiKeyOut:
    return await api_keys_service.revoke_key(get_context(), db, key_id)


@router.get("/{key_id}/usage", response_model=ApiKeyUsageOut)
async def key_usage(key_id: uuid.UUID, _: KeyManage, db: TenantDBSessionDep) -> ApiKeyUsageOut:
    return await api_keys_service.usage(get_context(), db, key_id)
