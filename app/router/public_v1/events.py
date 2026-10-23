# ruff: file-ignore[line-too-long]
"""``/public/v1/event_types`` and ``/public/v1/events`` (Epic 68, story 68.2): the public catalogue (``internal`` types
never listed) and the 30-day event read model for backfill, filtered to the types the caller's scopes allow."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Query

from app.core import public_events as registry
from app.core.context import get_context
from app.core.permissions import Principal
from app.core.public_api import public_router, scope_satisfied
from app.dependencies.public import PublicDBSessionDep, PublicPrincipalDep
from app.schemas.public_events import PublicPage
from app.schemas.public_v1.events import EventOut, EventQuery, EventTypeOut
from app.services import public_events_service

router = public_router("events")


@router.get("/event_types", response_model=list[EventTypeOut], response_model_by_alias=True)
async def event_types(_: PublicPrincipalDep) -> Any:
    return [
        EventTypeOut(
            type=t, version=s.version, description=s.description, scopes=list(s.scopes),
            schema=s.data_model.model_json_schema(), example=dict(s.example or {}),
        )
        for t, s in sorted(registry.catalogue().items())
    ]


def _key_allows(principal: Principal, event_type: str) -> bool:
    spec = registry.catalogue().get(event_type)
    return spec is not None and any(scope_satisfied(principal, k) for k in spec.scopes)


@router.get("/events", response_model=PublicPage[EventOut])
async def list_events(
    principal: PublicPrincipalDep, db: PublicDBSessionDep, type: Annotated[str | None, Query(max_length=60)] = None,
    since: datetime | None = None, subject_type: Annotated[str | None, Query(max_length=40)] = None, subject_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Any:  # fmt: skip
    q = EventQuery(type=type, subject_type=subject_type, subject_id=subject_id, occurred_after=since, limit=limit, cursor=cursor)
    page = await public_events_service.list_events(get_context(), db, q)
    if principal.type == "api_key":  # app principals are filtered by the read model itself
        page.data = [e for e in page.data if _key_allows(principal, e.type)]
    return page


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: uuid.UUID, principal: PublicPrincipalDep, db: PublicDBSessionDep) -> Any:
    ev = await public_events_service.get_event(get_context(), db, event_id)
    if principal.type == "api_key" and not _key_allows(principal, ev.type):
        from app.core.errors import NotFound

        raise NotFound("Event not found")
    return ev
