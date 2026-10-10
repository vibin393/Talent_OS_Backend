"""Portal notification inboxes (Epic 35 A7, FR-184): one router body mounted three times --
``/api/v1/client-portal/notifications``, ``/api/v1/vendor-portal/notifications`` and
``/api/v1/portal/work/notifications`` -- each with its portal dependency (fail-closed org GUCs). API keys never
reach these (portal sessions only)."""

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.client import get_client_db, require_client_user
from app.dependencies.vendor import get_vendor_db, require_vendor_user
from app.dependencies.worker import get_worker_db, require_worker
from app.services import portal_notifications_service as svc
from app.services.portal_notifications_service import (
    PortalInboxOut,
    PortalNotificationOut,
    PortalPreferenceIn,
    PortalPreferenceOut,
)


def _build(
    prefix: str,
    tag: str,
    kind: str,
    principal_dep: Callable[..., Any],
    db_dep: Callable[..., Any],
    id_of: Callable[[Any], uuid.UUID],
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/notifications", tags=[tag])
    Principal = Annotated[Any, Depends(principal_dep)]  # noqa: N806
    DB = Annotated[AsyncSession, Depends(db_dep)]  # noqa: N806

    @router.get("", response_model=PortalInboxOut)
    async def list_notifications(principal: Principal, db: DB, unread_only: bool = False) -> PortalInboxOut:
        return await svc.inbox(db, kind, id_of(principal), unread_only=unread_only)

    @router.post("/read-all", response_model=PortalInboxOut)
    async def read_all(principal: Principal, db: DB) -> PortalInboxOut:
        return await svc.mark_all_read(db, kind, id_of(principal))

    @router.get("/preferences", response_model=list[PortalPreferenceOut])
    async def get_preferences(principal: Principal, db: DB) -> list[PortalPreferenceOut]:
        return await svc.preferences(db, kind, id_of(principal))

    @router.patch("/preferences", response_model=list[PortalPreferenceOut])
    async def patch_preferences(
        body: PortalPreferenceIn, principal: Principal, db: DB
    ) -> list[PortalPreferenceOut]:
        return await svc.set_preference(db, kind, id_of(principal), body)

    @router.post("/{notification_id}/read", response_model=PortalNotificationOut)
    async def read_one(notification_id: uuid.UUID, principal: Principal, db: DB) -> PortalNotificationOut:
        return await svc.mark_read(db, kind, id_of(principal), notification_id)

    return router


def routers() -> list[APIRouter]:
    return [
        _build(
            "/client-portal",
            "client-portal",
            "client_user",
            require_client_user,
            get_client_db,
            lambda p: p.id,
        ),
        _build(
            "/vendor-portal",
            "vendor-portal",
            "vendor_user",
            require_vendor_user,
            get_vendor_db,
            lambda p: p.id,
        ),
        _build(
            "/portal/work",
            "portal-work",
            "candidate",
            require_worker,
            get_worker_db,
            lambda p: p.candidate_id,
        ),
    ]
