"""Notification endpoints (Epic 10, story 10.3). Always the caller's own notifications; polling only.

* ``GET /notifications?unread_only=&page=``, ``GET /notifications/unread-count`` (``no-store``)
* ``POST /notifications/{id}/read``, ``POST /notifications/read-all``, ``DELETE /notifications/{id}``
* ``GET/PUT /me/notification-preferences``
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.notifications import NotificationOut, PreferencesIn, PreferencesOut, UnreadCountOut
from app.services import notifications_service as svc

router = APIRouter(prefix="/notifications", tags=["notifications"])
me_router = APIRouter(prefix="/me", tags=["notifications"])
DB = TenantDBSessionDep


@router.get("", response_model=Page[NotificationOut])
async def list_notifications(
    _: CurrentUserDep, db: DB, params: Annotated[PageParams, Depends()], unread_only: bool = False
) -> Any:
    return await svc.list_notifications(get_context(), db, unread_only, params)


@router.get("/unread-count", response_model=UnreadCountOut)
async def unread_count(_: CurrentUserDep, db: DB, response: Response) -> Any:
    response.headers["Cache-Control"] = "no-store"
    return await svc.unread_count(get_context(), db)


@router.post("/read-all", response_model=UnreadCountOut)
async def read_all(_: CurrentUserDep, db: DB) -> Any:
    return await svc.mark_all_read(get_context(), db)


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(notification_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await svc.mark_read(get_context(), db, notification_id)


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(notification_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Response:
    await svc.delete_notification(get_context(), db, notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@me_router.get("/notification-preferences", response_model=PreferencesOut)
async def get_preferences(_: CurrentUserDep, db: DB) -> Any:
    return await svc.get_preferences(get_context(), db)


@me_router.put("/notification-preferences", response_model=PreferencesOut)
async def put_preferences(body: PreferencesIn, _: CurrentUserDep, db: DB) -> Any:
    return await svc.put_preferences(get_context(), db, body)
