"""Notification API models (Epic 10, story 10.3)."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Channel = Literal["in_app", "email", "both", "off"]


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    group: str
    title: str
    body: str | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    link_path: str | None
    actor_user_id: uuid.UUID | None
    read_at: datetime | None
    created_at: datetime


class UnreadCountOut(BaseModel):
    count: int  # capped at 100 (the UI shows 99+)
    latest_created_at: datetime | None


class PreferenceItemOut(BaseModel):
    type: str
    label: str
    group: str
    channel: Channel
    default_channel: Channel
    mandatory: bool


class DigestSettings(BaseModel):
    enabled: bool = False
    hour_local: Annotated[int, Field(ge=0, le=23)] = 8


class DigestSettingsOut(DigestSettings):
    timezone: str


class PreferencesOut(BaseModel):
    items: list[PreferenceItemOut]
    digest: DigestSettingsOut


class PreferenceItemIn(BaseModel):
    type: Annotated[str, Field(min_length=1, max_length=40)]
    channel: Channel


class PreferencesIn(BaseModel):
    items: Annotated[list[PreferenceItemIn], Field(max_length=100)] = Field(default_factory=list)
    digest: DigestSettings | None = None
