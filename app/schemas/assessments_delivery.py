# ruff: noqa: E501
"""Candidate-facing assessment models (Epic 40 A6, NFR-35): the Candidate Portal (``/portal/assessments``) and the
Secure Link page (``/public/links/{token}/assessment``).

Only stems, options and display code are delivered. These models NEVER carry ``answer_key``, ``rubric``,
``accepted``, ``regex``, ``keywords``, ``bug_lines``, ``order``, item ids or item-version ids:
``tests/modules/assessments/test_delivery.py`` walks every model below (and real responses) against
``FORBIDDEN_DELIVERY_FIELDS``. Option and line ids are opaque per attempt. Registered as ``worker`` portal schemas
(the Candidate Portal principal is the worker organisation scope).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.field_visibility import register_portal_schema
from app.core.money import DecimalStr

FORBIDDEN_DELIVERY_FIELDS = frozenset(
    {
        "answer_key", "rubric", "accepted", "regex", "keywords", "bug_lines", "order", "correct", "item_id",
        "item_version_id", "version_id", "assessment_version_id", "score", "auto_score", "final_score",
        "auto_confidence", "needs_review", "min_keyword_score", "tolerance_abs", "tolerance_pct", "blanks_key",
    }
)  # fmt: skip

SignalKind = Literal["tab_switch", "focus_loss", "copy", "paste", "fullscreen_exit"]
CLIENT_SIGNAL_KINDS: tuple[str, ...] = ("tab_switch", "focus_loss", "copy", "paste", "fullscreen_exit")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_portal_schema("worker")
class NoticeOut(BaseModel):
    notice_id: uuid.UUID | None
    title: str
    body_md: str


@register_portal_schema("worker")
class PreStartSectionOut(BaseModel):
    key: str
    title: str
    item_count: int
    time_limit_minutes: int | None


@register_portal_schema("worker")
class PreStartOut(BaseModel):
    invitation_id: uuid.UUID
    assessment_name: str
    instructions: str | None
    status: str
    deadline_at: datetime
    time_limit_minutes: int | None
    accommodation_factor: DecimalStr
    screen_reader_mode: bool
    sections: list[PreStartSectionOut]
    notice: NoticeOut
    logged_signals: list[str]
    has_active_attempt: bool
    delivery: str
    take_url: str | None


@register_portal_schema("worker")
class PortalInvitationOut(BaseModel):
    invitation_id: uuid.UUID
    assessment_name: str
    status: str
    deadline_at: datetime


@register_portal_schema("worker")
class ChoiceOut(BaseModel):
    id: str
    text: str


@register_portal_schema("worker")
class DeliveryItem(BaseModel):
    position: int
    section_key: str
    kind: str
    stem: str
    options: list[ChoiceOut] | None = None
    code: str | None = None
    language: str | None = None
    lines: list[ChoiceOut] | None = None
    blanks: int | None = None
    points: DecimalStr


@register_portal_schema("worker")
class SectionStateOut(BaseModel):
    key: str
    title: str
    deadline_at: datetime | None
    closed: bool
    positions: list[int]


@register_portal_schema("worker")
class AttemptView(BaseModel):
    session_id: str | None
    server_now: datetime
    deadline_at: datetime
    remaining_seconds: int
    status: str
    current_section: str | None
    screen_reader_mode: bool
    sections: list[SectionStateOut]
    items: list[DeliveryItem]
    answers: dict[int, Any]


@register_portal_schema("worker")
class SaveOut(BaseModel):
    saved: int
    server_now: datetime
    deadline_at: datetime
    remaining_seconds: int


@register_portal_schema("worker")
class SubmitOut(BaseModel):
    status: str
    submitted_at: datetime


@register_portal_schema("worker")
class SignalsAcceptedOut(BaseModel):
    accepted: int
    dropped: int


class StartIn(_In):
    notice_id: uuid.UUID | None = None
    agreed: bool


class AnswerIn(_In):
    position: int = Field(ge=0, le=10_000)
    response: dict[str, Any] | None


class SaveIn(_In):
    client_seq: int = Field(ge=0)
    answers: Annotated[list[AnswerIn], Field(max_length=200)]


class SignalIn(_In):
    kind: SignalKind
    client_ts: datetime | None = None
    detail: dict[str, Any] | None = None


class SignalsIn(_In):
    signals: Annotated[list[SignalIn], Field(max_length=200)]


__all__ = [
    "CLIENT_SIGNAL_KINDS",
    "FORBIDDEN_DELIVERY_FIELDS",
    "AnswerIn",
    "AttemptView",
    "ChoiceOut",
    "DeliveryItem",
    "NoticeOut",
    "PortalInvitationOut",
    "PreStartOut",
    "PreStartSectionOut",
    "SaveIn",
    "SaveOut",
    "SectionStateOut",
    "SignalIn",
    "SignalsAcceptedOut",
    "SignalsIn",
    "StartIn",
    "SubmitOut",
]
