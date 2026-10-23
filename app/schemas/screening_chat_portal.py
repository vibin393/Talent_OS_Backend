# ruff: file-ignore[line-too-long]
"""Candidate-facing projection of a Screening Conversation (Epic 57 A7): Candidate Portal chat and the Secure-Link chat.

Never carries confidence, review state, flags, summaries, run ids or internal names beyond the recruiter's display name.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortalChatTurn(_Out):
    seq: int
    author: Literal["assistant", "you", "recruiter"]
    body: str
    sent_at: datetime


class PortalChatNotice(_Out):
    notice_id: uuid.UUID | None
    title: str
    body_md: str


class PortalScreeningChat(_Out):
    id: uuid.UUID
    title: str
    status: Literal["waiting_for_consent", "open", "paused", "ended"]
    recruiter_name: str
    consent_needed: bool
    can_reply: bool
    notice: PortalChatNotice | None = None
    turns: list[PortalChatTurn] = Field(default_factory=list)


class PortalScreeningChatSummary(_Out):
    id: uuid.UUID
    title: str
    status: Literal["waiting_for_consent", "open", "paused", "ended"]
    recruiter_name: str
    last_message_at: datetime | None


class ChatConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["agree", "decline"]
    notice_id: uuid.UUID | None = None


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=2000)


__all__ = [
    "ChatConsentIn",
    "ChatMessageIn",
    "PortalChatNotice",
    "PortalChatTurn",
    "PortalScreeningChat",
    "PortalScreeningChatSummary",
]
