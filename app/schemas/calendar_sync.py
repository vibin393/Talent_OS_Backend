"""Calendar Sync DTOs (Epic 72). No tokens, no provider event content, no user emails in admin views."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CalendarSyncSettingsOut(BaseModel):
    enabled: bool
    move_policy: Literal["restore", "keep_as_proposal"]
    poll_minutes: int
    restore_limit_per_hour: int
    org_source_enabled: bool
    org_source_installed: bool = False
    opted_out_count: int = 0


class CalendarSyncSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    move_policy: Literal["restore", "keep_as_proposal"] | None = None
    poll_minutes: int | None = Field(default=None, ge=5, le=60)
    restore_limit_per_hour: int | None = Field(default=None, ge=1, le=10)
    org_source_enabled: bool | None = None


ChannelStatus = Literal["active", "renewing", "polling", "stopped", "failed"]


class MyChannelOut(BaseModel):
    connection_id: uuid.UUID
    provider: str
    status: ChannelStatus | Literal["none"]
    last_notification_at: datetime | None = None
    last_sync_at: datetime | None = None
    poll_minutes: int


class MySyncOut(BaseModel):
    tenant_enabled: bool
    channels: list[MyChannelOut]
    org_source_enabled: bool
    free_busy_opted_out: bool


class OptOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opted_out: bool


class ParticipantSyncState(BaseModel):
    participant_id: uuid.UUID | None
    user_id: uuid.UUID
    name: str
    state: Literal["syncing", "polling", "not_syncing", "not_connected"]


class EventLinkOut(BaseModel):
    id: uuid.UUID
    role: Literal["organizer", "attendee"]
    participant_id: uuid.UUID | None
    state: Literal["in_sync", "moved", "deleted", "restoring", "conflict", "detached"]
    seen_start: datetime | None
    seen_end: datetime | None
    organizer_name: str | None = None


class InterviewSyncOut(BaseModel):
    interview_id: uuid.UUID
    move_policy: Literal["restore", "keep_as_proposal"]
    participants: list[ParticipantSyncState]
    links: list[EventLinkOut]


class ChannelAdminOut(BaseModel):
    id: uuid.UUID
    person: str
    provider: str
    status: ChannelStatus
    last_notification_at: datetime | None
    last_sync_at: datetime | None


class ActionOut(BaseModel):
    status: str
