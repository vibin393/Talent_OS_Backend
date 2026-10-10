"""Activity / timeline API models (Epic 2, story 2.3)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

ActivityKind = Literal["note", "call", "meeting", "email", "system", "email_reply", "sms", "whatsapp"]  # Epic 33: +3
UserActivityKind = Literal["note", "call", "meeting"]
MAX_BODY = 20_000

Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_BODY)]


class Participant(BaseModel):
    kind: Literal["user", "contact", "other"] = "other"
    id: uuid.UUID | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class ActivityCreate(BaseModel):
    """Body may contain mentions as ``@[Display Name](user:<uuid>)`` markup."""

    kind: UserActivityKind = "note"
    body: Body
    occurred_at: datetime | None = None  # default now; calls/meetings may be logged after the fact
    is_private: bool = False
    participants: Annotated[list[Participant], Field(max_length=50)] = Field(default_factory=list)


class ActivityUpdate(BaseModel):
    body: Body | None = None
    is_private: bool | None = None


class ActivityOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    kind: ActivityKind
    body: str
    occurred_at: datetime
    author_id: uuid.UUID | None
    author_name: str | None = None
    is_private: bool
    participants: list[Participant]
    mentions: list[uuid.UUID]
    data: dict[str, Any] | None = None
    edited_at: datetime | None
    created_at: datetime
    can_edit: bool = False


class TimelinePage(BaseModel):
    """Newest first; pass ``next_cursor`` back as ``cursor`` for the next page (None = last page)."""

    items: list[ActivityOut]
    next_cursor: str | None = None
