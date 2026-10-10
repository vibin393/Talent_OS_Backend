"""Tenant-side platform notices and the sub-processor list (Epic 12, FR-71/FR-72)."""

import uuid

from fastapi import APIRouter, status

from app.core.context import get_context
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.platform import PlatformNoticeOut, SubProcessorOut
from app.services import platform_service

router = APIRouter(tags=["notices"])


@router.get("/notices", response_model=list[PlatformNoticeOut])
async def list_notices(_: CurrentUserDep, db: TenantDBSessionDep) -> list[PlatformNoticeOut]:
    return await platform_service.notices_for(get_context(), db)


@router.post("/notices/{notice_id}/ack", status_code=status.HTTP_204_NO_CONTENT)
async def ack_notice(notice_id: uuid.UUID, _: CurrentUserDep, db: TenantDBSessionDep) -> None:
    await platform_service.ack_notice(get_context(), db, notice_id)


@router.get("/sub-processors", response_model=list[SubProcessorOut])
async def sub_processors(_: CurrentUserDep) -> list[SubProcessorOut]:
    return await platform_service.list_sub_processors(include_planned=True)
