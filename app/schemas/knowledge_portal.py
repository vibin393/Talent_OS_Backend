# ruff: file-ignore[line-too-long]
"""Worker Portal projection of Onboarding help (Epic 62 B7; ``/portal/work/help``).

Never carries run ids, outcomes, topics, internal notes or other people's data: only the asker's own messages, the
assistant's / coordinator's replies with their citation chips and the coordinator's display name.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.field_visibility import register_portal_schema


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_portal_schema("worker")
class PortalCitation(BaseModel):
    label: str
    kind: Literal["knowledge", "task", "case"]


@register_portal_schema("worker")
class PortalHelpMessage(BaseModel):
    id: uuid.UUID
    seq: int
    author: Literal["you", "assistant", "coordinator", "system"]
    text: str
    citations: list[PortalCitation] = Field(default_factory=list)
    feedback: Literal["helpful", "not_helpful", "incorrect"] | None = None
    sent_at: datetime


@register_portal_schema("worker")
class PortalHelpNotice(BaseModel):
    notice_id: uuid.UUID | None
    title: str
    body_md: str


@register_portal_schema("worker")
class PortalHelpThreadSummary(BaseModel):
    id: uuid.UUID
    status: Literal["open", "answered", "handed_off", "closed"]
    first_question: str
    last_message_at: datetime | None


@register_portal_schema("worker")
class PortalHelpThread(BaseModel):
    id: uuid.UUID
    status: Literal["open", "answered", "handed_off", "closed"]
    coordinator_name: str
    can_reply: bool
    messages: list[PortalHelpMessage] = Field(default_factory=list)


@register_portal_schema("worker")
class PortalHelpState(BaseModel):
    available: bool
    consent_needed: bool
    coordinator_name: str
    notice: PortalHelpNotice | None = None
    threads: list[PortalHelpThreadSummary] = Field(default_factory=list)


class PortalAskIn(_In):
    text: str = Field(min_length=1, max_length=2000)


class PortalConsentIn(_In):
    decision: Literal["accept", "decline"] = "accept"
    notice_id: uuid.UUID | None = None


class PortalFeedbackIn(_In):
    feedback: Literal["helpful", "not_helpful", "incorrect"]
    note: str | None = Field(default=None, max_length=500)


__all__ = [
    "PortalAskIn",
    "PortalCitation",
    "PortalConsentIn",
    "PortalFeedbackIn",
    "PortalHelpMessage",
    "PortalHelpNotice",
    "PortalHelpState",
    "PortalHelpThread",
    "PortalHelpThreadSummary",
]
