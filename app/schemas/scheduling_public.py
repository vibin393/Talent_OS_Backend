# ruff: file-ignore[line-too-long]
"""Candidate-facing projection of a scheduling offer (Epic 59, Secure Link ``slot_pick`` and the Candidate Portal).

No internal ids beyond the offer id (portal), no participant names or emails, no availability details: only the
offered times (UTC; the page renders them in the chosen time zone) and the request state."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PickStatus = Literal["open", "picked", "booked", "counter_proposed", "expired", "closed"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicSlot(BaseModel):
    index: int
    start: datetime
    end: datetime


class SlotPickView(BaseModel):
    offer_id: uuid.UUID | None = None
    tenant: str | None
    demand_title: str | None
    stage_name: str | None
    duration: int
    mode: str | None
    slots: list[PublicSlot]
    candidate_timezone: str
    status: PickStatus
    expires_at: datetime | None
    booked_start: datetime | None = None
    picked_index: int | None = None
    notice: Literal["slot_taken", "new_options"] | None = None


class PickIn(_In):
    index: Annotated[int, Field(ge=0, le=9)]
    pick_id: uuid.UUID


class CounterWindowIn(_In):
    start: datetime
    end: datetime


class CounterIn(_In):
    windows: Annotated[list[CounterWindowIn], Field(min_length=1, max_length=3)]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class TimezoneIn(_In):
    tz: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class PickResultOut(BaseModel):
    status: PickStatus
    deduplicated: bool = False
    view: SlotPickView


class PortalRescheduleIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class PortalRescheduleOut(BaseModel):
    status: str


__all__ = [
    "CounterIn",
    "CounterWindowIn",
    "PickIn",
    "PickResultOut",
    "PortalRescheduleIn",
    "PortalRescheduleOut",
    "PublicSlot",
    "SlotPickView",
    "TimezoneIn",
]
