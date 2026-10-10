# ruff: noqa: E501
"""``/public/v1/activities`` (Epic 67, spec A4): logged calls, meetings and emails (never sends anything)."""

import uuid
from typing import Annotated, Any

from fastapi import Query, status

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Idem, Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import ActivityCreateIn, NoteOut
from app.services import public_api_service as svc

router = public_router("activities")


@router.get("/activities", response_model=Page[NoteOut])
async def list_activities(
    _: scope("activities:read"), db: PublicDBSessionDep, page: Listing, entity_type: Annotated[str, Query(max_length=30)],
    entity_id: uuid.UUID,
) -> dict[str, Any]:  # fmt: skip
    return await svc.engagement.list_entries(db, kinds=("note", "call", "meeting"), entity_type=entity_type, entity_id=entity_id, **page.kw())


@router.post("/activities", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def create_activity(body: ActivityCreateIn, _: scope("activities:write"), db: PublicDBSessionDep, idem: Idem) -> Any:
    return await idem.run(lambda: svc.engagement.create_activity(db, body))
