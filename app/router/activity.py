"""Activity endpoints (Epic 2, story 2.3).

* ``GET  /api/v1/{entity}/{id}/timeline`` -- newest first, cursor pagination.
* ``POST /api/v1/{entity}/{id}/activities`` -- add a note / logged call or meeting.
* ``PATCH /api/v1/activities/{activity_id}`` -- author edit within 24 h.

``{entity}`` is a registered plural (``clients``, ``contacts``, ``candidates``, ...); access to the record is
decided by its owning service (``activity_service.register_entity``). The generic routes must be mounted
after every specific router (see ``app.router.get_layered_routers``).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.core.context import get_context
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.activity import ActivityCreate, ActivityOut, ActivityUpdate, TimelinePage
from app.services import activity_service

router = APIRouter(tags=["activity"])

EntitySegment = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]


@router.patch("/activities/{activity_id}", response_model=ActivityOut)
async def update_activity(
    activity_id: uuid.UUID, body: ActivityUpdate, _: CurrentUserDep, db: TenantDBSessionDep
) -> ActivityOut:
    return await activity_service.update_activity(get_context(), db, activity_id, body)


@router.get("/{entity}/{entity_id}/timeline", response_model=TimelinePage)
async def timeline(
    entity: EntitySegment,
    entity_id: uuid.UUID,
    _: CurrentUserDep,
    db: TenantDBSessionDep,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=activity_service.MAX_LIMIT)] = activity_service.DEFAULT_LIMIT,
) -> TimelinePage:
    return await activity_service.timeline(get_context(), db, entity, entity_id, cursor, limit)


@router.post(
    "/{entity}/{entity_id}/activities", response_model=ActivityOut, status_code=status.HTTP_201_CREATED
)
async def add_activity(
    entity: EntitySegment,
    entity_id: uuid.UUID,
    body: ActivityCreate,
    _: CurrentUserDep,
    db: TenantDBSessionDep,
) -> ActivityOut:
    return await activity_service.add_activity(get_context(), db, entity, entity_id, body)
