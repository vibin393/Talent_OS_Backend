# ruff: noqa: E501
"""Public Event schemas (Epic 66, AD-79): the replay / backfill read model of ``outbox_events`` (FR-334/350).

``EventOut`` mirrors the frozen wire envelope (``public_events_service.envelope``). ``PublicPage`` comes from
``app.core.public_api`` (§0.7) when present; a minimal compatible model (data, next_cursor, has_more) otherwise.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

try:  # pragma: no cover - depends on the coordinator's §0.7 module
    from app.core.public_api import PublicPage
except ImportError:  # pragma: no cover

    class PublicPage[T](BaseModel):  # type: ignore[no-redef]
        data: list[T]
        next_cursor: str | None = None
        has_more: bool = False


class EventQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str | None = Field(default=None, max_length=60)
    subject_type: str | None = Field(default=None, max_length=40)
    subject_id: uuid.UUID | None = None
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=50, ge=1, le=100)


class EventTenantRef(BaseModel):
    id: uuid.UUID
    slug: str


class EventSubject(BaseModel):
    type: str
    id: uuid.UUID


class EventOut(BaseModel):
    id: uuid.UUID
    type: str
    version: int
    api_version: str = "v1"
    occurred_at: datetime
    sequence: int
    tenant_ref: EventTenantRef
    livemode: bool
    subject: EventSubject
    data: dict[str, Any]


__all__ = ["EventOut", "EventQuery", "EventSubject", "EventTenantRef", "PublicPage"]
