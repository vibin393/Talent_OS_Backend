# ruff: file-ignore[line-too-long]
"""Recruitment Copilot DTOs (Epic 54, AD-64). Admin usage DTOs carry metadata only -- never question or answer text
(coordinator decision 6 / C-14)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

EntityType = Literal[
    "candidate",
    "demand",
    "application",
    "client",
    "contact",
    "placement",
    "interview",
    "worker",
    "offer",
    "project",
]
ListKind = Literal["candidates", "demands", "pipeline", "interviews", "placements", "tasks"]
FilterValue = (
    bool
    | int
    | Annotated[str, StringConstraints(max_length=200)]
    | list[Annotated[str, StringConstraints(max_length=200)]]
)
UnavailableReason = Literal["no_permission", "not_enabled", "kill_switch", "global_kill", "budget_exhausted"]
Rating = Literal["helpful", "unhelpful"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListContext(_In):
    kind: ListKind
    filters: dict[Annotated[str, StringConstraints(max_length=60)], FilterValue] = Field(default_factory=dict)
    selected_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)

    @field_validator("filters")
    @classmethod
    def _filters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 20:
            msg = "at most 20 filters"
            raise ValueError(msg)
        for v in value.values():
            if isinstance(v, list) and len(v) > 20:
                msg = "at most 20 values per filter"
                raise ValueError(msg)
        return value


class PageContextIn(_In):
    route: Annotated[str, StringConstraints(max_length=200, pattern=r"^/")]
    entity_type: EntityType | None = None
    entity_id: uuid.UUID | None = None
    list: ListContext | None = None
    label: Annotated[str, StringConstraints(max_length=120)] | None = None


class ConversationCreate(_In):
    agent_key: Annotated[str, StringConstraints(max_length=40)] = "copilot"
    page_context: PageContextIn | None = None


class ConversationRename(_In):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ConversationQuery(_In):
    status: Literal["open", "closed"] | None = None
    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int = Field(default=20, ge=1, le=100)


class ConversationSummaryOut(BaseModel):
    id: uuid.UUID
    agent_key: str
    mode_label: str
    title: str | None
    status: str
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    expires_at: datetime


class ConversationOut(ConversationSummaryOut):
    context_ref: dict[str, Any] | None


class MessageIn(_In):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    page_context: PageContextIn | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    seq: int
    role: Literal["user", "assistant", "event"]
    content: dict[str, Any]
    run_id: uuid.UUID | None
    redacted: bool
    created_at: datetime


class TurnOut(BaseModel):
    message: MessageOut
    run_id: uuid.UUID | None
    stream_url: str | None
    status: str
    reason: str | None = None


class FeedbackIn(_In):
    rating: Rating
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None


class FeedbackOut(BaseModel):
    run_id: uuid.UUID
    rating: Rating
    comment: str | None
    updated_at: datetime


class ModeOut(BaseModel):
    agent_key: str
    label: str


class CopilotStatusOut(BaseModel):
    available: bool
    reason: UnavailableReason | None
    questions_left_this_hour: int
    questions_per_hour: int
    retention_days: int
    allow_feedback_comments: bool
    modes: list[ModeOut]


class CopilotSettingsIn(_In):
    retention_days: int | None = Field(default=None, ge=7, le=365)
    questions_per_hour: int | None = Field(default=None, ge=5, le=300)
    max_open_conversations: int | None = Field(default=None, ge=5, le=200)
    allow_feedback_comments: bool | None = None
    draft_expiry_hours: int | None = Field(default=None, ge=1, le=72)


class CopilotSettingsOut(BaseModel):
    retention_days: int
    questions_per_hour: int
    max_open_conversations: int
    allow_feedback_comments: bool
    draft_expiry_hours: int
    enabled: bool
    agentic_enabled: bool
    updated_at: datetime | None


class UsageQuery(_In):
    date_from: date | None = None
    date_to: date | None = None


class UsageDayOut(BaseModel):
    """One day of Copilot usage -- counts, tokens and cost only (no text)."""

    day: date
    conversations: int
    turns: int
    active_users: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    helpful: int
    unhelpful: int
    helpful_pct: float | None
    failures: dict[str, int]


class UsageCommentOut(BaseModel):
    """A feedback comment: the user's own text, shared with AI administrators (the UI says so)."""

    run_id: uuid.UUID
    rating: Rating
    comment: str
    created_at: datetime


class CopilotUsageOut(BaseModel):
    date_from: date
    date_to: date
    days: list[UsageDayOut]
    comments: list[UsageCommentOut]


__all__ = [
    "ConversationCreate",
    "ConversationOut",
    "ConversationQuery",
    "ConversationRename",
    "ConversationSummaryOut",
    "CopilotSettingsIn",
    "CopilotSettingsOut",
    "CopilotStatusOut",
    "CopilotUsageOut",
    "FeedbackIn",
    "FeedbackOut",
    "ListContext",
    "MessageIn",
    "MessageOut",
    "ModeOut",
    "PageContextIn",
    "TurnOut",
    "UsageCommentOut",
    "UsageDayOut",
    "UsageQuery",
]
