# ruff: file-ignore[line-too-long]
"""Screening agent contract DTOs (Epic 53 contract layer, spec 0.12; filled by Epic 57, used by 57 and 58).
Frozen; field sets pinned by ``tests/contracts/signatures.json``."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.pipeline import MappedField


class ExtractAnswersIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_id: uuid.UUID
    questionnaire_id: uuid.UUID
    questionnaire_version: int
    transcript_id: uuid.UUID
    source: Literal["text", "voice"]
    run_id: uuid.UUID | None = None


class ExtractedAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    question_id: uuid.UUID
    answer_text: str
    normalized: dict[str, Any] | None = None
    confidence: Decimal = Field(ge=0, le=1)
    segment_refs: tuple[int, ...] = ()
    start_ms: int | None = None
    flags: tuple[str, ...] = ()


class ExtractedAnswersOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_id: uuid.UUID
    transcript_id: uuid.UUID
    answers: tuple[ExtractedAnswer, ...]
    summary: str
    unanswered: tuple[uuid.UUID, ...]
    ai_action_id: uuid.UUID | None = None


# ================================================================ Epic 57 additions (the three DTOs above are frozen)
AnswerType = Literal["text", "number", "boolean", "choice", "money", "date", "rating"]
Unit = Literal["days", "weeks", "months", "years", "hours", "km", "lpa", "percent"]
QuestionOrigin = Literal["default", "skill", "manual", "ai_generated"]
KnockoutOp = Literal["lt", "lte", "gt", "gte", "eq", "neq", "in", "not_in"]
Channel = Literal["portal_chat", "link_chat", "email", "whatsapp"]
StartChannel = Literal["auto", "portal_chat", "link_chat", "email", "whatsapp"]
ConversationStatus = Literal[
    "invited", "consented", "in_progress", "paused", "completed", "reviewed", "expired", "withdrawn", "handed_off",
    "taken_over", "consent_refused", "cancelled",
]  # fmt: skip
HandoffReason = Literal[
    "candidate_request", "compensation", "ungrounded_question", "language", "model_failure", "alternative_process", "other",
]  # fmt: skip
AnswerStatus = Literal["unconfirmed", "accepted", "edited", "rejected"]
MAX_QUESTIONS = 20
Key40 = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]
Option = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnockoutRule(_In):
    """A knockout criterion only FLAGS an answer (flag ``knockout``); no setting can make it reject (FR-289)."""

    op: KnockoutOp
    value: Any


class QuestionDraft(_In):
    """A question as an editor submits it (``id`` optional: new questions get a stable id)."""

    id: uuid.UUID | None = None
    key: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,60}$")]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    type: AnswerType
    unit: Unit | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    options: list[Option] | None = Field(default=None, max_length=50)
    required: bool = False
    maps_to: MappedField | None = None
    min: float | None = None
    max: float | None = None
    knockout: KnockoutRule | None = None
    origin: QuestionOrigin = "manual"
    accepted_by: uuid.UUID | None = None


class AgentQuestion(QuestionDraft):
    """Superset of the P1 ``Question``; ``id`` is stable across versions."""

    id: uuid.UUID


class VersionCreate(_In):
    questions: list[QuestionDraft] | None = Field(default=None, min_length=1, max_length=MAX_QUESTIONS)
    clarifications: (
        dict[Key40, dict[Key40, Annotated[str, StringConstraints(min_length=1, max_length=500)]]] | None
    ) = None
    change_note: str | None = Field(default=None, max_length=500)


class VersionUpdate(VersionCreate):
    pass


class VersionOut(BaseModel):
    id: uuid.UUID
    questionnaire_id: uuid.UUID
    questionnaire_name: str | None = None
    version: int
    status: Literal["draft", "approved", "retired"]
    questions: list[AgentQuestion]
    clarifications: dict[str, dict[str, str]]
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    change_note: str | None
    created_at: datetime


class GenerateQuestionsIn(_In):
    demand_id: uuid.UUID


class GeneratedQuestionsOut(BaseModel):
    questions: list[AgentQuestion]


class BindingIn(_In):
    scope_kind: Literal["tenant", "client", "demand"]
    scope_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _scope(self) -> "BindingIn":
        if (self.scope_kind == "tenant") != (self.scope_id is None):
            msg = "scope_id is required for client / demand bindings and must be empty for tenant"
            raise ValueError(msg)
        return self


class BindingOut(BaseModel):
    id: uuid.UUID
    questionnaire_id: uuid.UUID
    scope_kind: str
    scope_id: uuid.UUID | None


# ---------------------------------------------------------------- conversations
class ConversationStartIn(_In):
    channel: StartChannel = "auto"


class TurnOut(BaseModel):
    id: uuid.UUID
    seq: int
    direction: Literal["inbound", "outbound"]
    author: Literal["agent", "candidate", "recruiter", "system"]
    kind: str
    channel: str
    body: str
    question_ids: list[uuid.UUID]
    citations: list[str]
    template_key: str | None
    created_at: datetime


class ConversationOut(BaseModel):
    id: uuid.UUID
    human_id: str
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    demand_id: uuid.UUID
    screening_id: uuid.UUID | None
    questionnaire_version_id: uuid.UUID
    questionnaire_label: str | None = None
    channel: Channel
    status: ConversationStatus
    current_run_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    owner_name: str | None = None
    invited_at: datetime | None
    consented_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    wa_window_open: bool
    reminders_sent: int
    handoff_reason: str | None
    taken_over_by: uuid.UUID | None
    required_count: int
    answered_count: int
    summary: str | None
    gaps: list[Any]
    flags: list[Any]
    created_at: datetime


class ConversationDetailOut(ConversationOut):
    turns: list[TurnOut]


class RecruiterMessageIn(_In):
    body: str = Field(min_length=1, max_length=2000)


class ConversationActionIn(_In):
    reason: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------- review
class AnswerOut(BaseModel):
    id: uuid.UUID
    question_id: uuid.UUID
    question_label: str
    question_type: str
    unit: str | None = None
    required: bool
    source: Literal["text", "voice"]
    answer_text: str | None
    normalized: dict[str, Any] | None
    confidence: Decimal | None
    low_confidence: bool
    flags: list[str]
    knockout: KnockoutRule | None = None
    status: AnswerStatus
    edited_value: dict[str, Any] | None
    segment_refs: list[int]
    start_ms: int | None
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None


class ReviewOut(BaseModel):
    conversation_id: uuid.UUID | None
    voice_session_id: uuid.UUID | None
    application_id: uuid.UUID
    screening_id: uuid.UUID | None
    status: str
    threshold: Decimal
    summary: str | None
    gaps: list[Any]
    suggested_next: str | None
    unanswered_required: list[uuid.UUID]
    answers: list[AnswerOut]
    can_complete: bool


class AnswerDecisionIn(_In):
    decision: Literal["accept", "edit", "reject"]
    edited_value: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _edit(self) -> "AnswerDecisionIn":
        if self.decision == "edit" and self.edited_value is None:
            msg = "edited_value is required to edit an answer"
            raise ValueError(msg)
        return self


class ReviewCompleteIn(_In):
    conversation_id: uuid.UUID | None = None
    voice_session_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> "ReviewCompleteIn":
        if (self.conversation_id is None) == (self.voice_session_id is None):
            msg = "give exactly one of conversation_id / voice_session_id"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------- settings
class ScreeningAgentSettingsOut(BaseModel):
    expiry_days: int
    reminder_after_hours: int
    max_reminders: int
    questions_per_turn: int
    low_confidence_threshold: Decimal
    stop_words: list[str]
    whatsapp_invite_template_id: uuid.UUID | None
    default_channel: StartChannel
    allow_campus_candidates: bool


class ScreeningAgentSettingsIn(_In):
    expiry_days: int | None = Field(default=None, ge=1, le=14)
    reminder_after_hours: int | None = Field(default=None, ge=4, le=72)
    max_reminders: int | None = Field(default=None, ge=0, le=2)
    questions_per_turn: int | None = Field(default=None, ge=1, le=3)
    low_confidence_threshold: Decimal | None = Field(default=None, ge=Decimal("0.5"), le=Decimal("0.95"))
    stop_words: (
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]] | None
    ) = Field(default=None, max_length=30)
    whatsapp_invite_template_id: uuid.UUID | None = None
    default_channel: StartChannel | None = None
    allow_campus_candidates: bool | None = None


# ---------------------------------------------------------------- model-facing schemas
class ScreeningResult(BaseModel):
    """``emit_result`` schema of the Screening Agent (ids and counts only)."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["completed", "handed_off", "waiting", "nothing_to_do", "needs_human"]
    summary: str = Field(default="", max_length=2000)
    answered: int = Field(default=0, ge=0, le=MAX_QUESTIONS)
    unanswered_required: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    handed_off: bool = False


class ExtractionItem(BaseModel):
    """One extracted answer (fast tier). There is deliberately no field describing the person."""

    model_config = ConfigDict(extra="forbid")

    question_id: uuid.UUID
    quote: str = Field(max_length=300)
    value: Any = None
    confidence: float = Field(ge=0, le=1)
    segment_refs: list[int] = Field(default_factory=list, max_length=20)


class ExtractionOut(BaseModel):
    """``screening_extract.v1`` output. ``extra="forbid"``: tone / emotion / personality fields are refused."""

    model_config = ConfigDict(extra="forbid")

    answers: list[ExtractionItem] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    unanswered: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_QUESTIONS)

    @field_validator("answers")
    @classmethod
    def _unique(cls, value: list[ExtractionItem]) -> list[ExtractionItem]:
        seen: set[uuid.UUID] = set()
        out: list[ExtractionItem] = []
        for item in value:
            if item.question_id not in seen:
                seen.add(item.question_id)
                out.append(item)
        return out


__all__ = [
    "AgentQuestion",
    "AnswerDecisionIn",
    "AnswerOut",
    "BindingIn",
    "BindingOut",
    "ConversationActionIn",
    "ConversationDetailOut",
    "ConversationOut",
    "ConversationStartIn",
    "ExtractAnswersIn",
    "ExtractedAnswer",
    "ExtractedAnswersOut",
    "ExtractionItem",
    "ExtractionOut",
    "GenerateQuestionsIn",
    "GeneratedQuestionsOut",
    "KnockoutRule",
    "QuestionDraft",
    "RecruiterMessageIn",
    "ReviewCompleteIn",
    "ReviewOut",
    "ScreeningAgentSettingsIn",
    "ScreeningAgentSettingsOut",
    "ScreeningResult",
    "TurnOut",
    "VersionCreate",
    "VersionOut",
    "VersionUpdate",
]


class ChannelOption(BaseModel):
    channel: Channel
    available: bool
    reason: str | None = None


class StartOptionsOut(BaseModel):
    """What the "Start AI screening" dialog needs: channels (with disabled reasons) and the questionnaire."""

    channels: list[ChannelOption]
    best_channel: Channel | None
    questionnaire_id: uuid.UUID | None
    questionnaire_name: str | None
    version_id: uuid.UUID | None
    version: int | None
    blocked_reason: str | None = None


__all__ += ["ChannelOption", "StartOptionsOut"]
