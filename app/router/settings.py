"""Tenant settings hub (Epic 12 story 12.8, FR-74): ``settings:manage``."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.settings import (
    CompanyUpdate,
    IdPrefixesUpdate,
    LinkExpiriesUpdate,
    RegionalUpdate,
    SettingsCatalogEntry,
    SettingsOut,
)
from app.services import settings_service

router = APIRouter(prefix="/settings", tags=["settings"])
SettingsManage = Annotated[Principal, Depends(require("settings:manage"))]


@router.get("", response_model=list[SettingsCatalogEntry])
async def get_settings_catalog(_: SettingsManage, db: TenantDBSessionDep) -> list[dict[str, Any]]:
    return await settings_service.catalog(get_context(), db)


@router.get("/sections", response_model=SettingsOut)
async def get_settings_sections(_: SettingsManage, db: TenantDBSessionDep) -> dict[str, Any]:
    return await settings_service.hub(get_context(), db)


@router.patch("/company", response_model=dict[str, Any])
async def patch_company(body: CompanyUpdate, _: SettingsManage, db: TenantDBSessionDep) -> dict[str, Any]:
    return await settings_service.update_company(get_context(), db, body.model_dump(mode="json", exclude_unset=True))


@router.patch("/regional", response_model=dict[str, Any])
async def patch_regional(body: RegionalUpdate, _: SettingsManage, db: TenantDBSessionDep) -> dict[str, Any]:
    return await settings_service.update_regional(get_context(), db, body.model_dump(exclude_unset=True))


@router.patch("/id-prefixes", response_model=dict[str, Any])
async def patch_id_prefixes(body: IdPrefixesUpdate, _: SettingsManage, db: TenantDBSessionDep) -> dict[str, Any]:
    return await settings_service.update_id_prefixes(get_context(), db, body.model_dump(exclude_unset=True))


@router.patch("/link-expiries", response_model=dict[str, Any])
async def patch_link_expiries(body: LinkExpiriesUpdate, _: SettingsManage, db: TenantDBSessionDep) -> dict[str, Any]:
    return await settings_service.update_link_expiries(get_context(), db, body.model_dump(exclude_unset=True))
