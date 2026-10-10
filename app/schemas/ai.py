import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AIFeature = Literal[
    "demand_intake", "jd_generation", "resume_parsing", "nl_search", "matching", "ai_screening"
]
AIProcessingBasis = Literal["consent_required", "notice_based"]
AIDecision = Literal["accepted", "edited", "rejected"]


class AISettingsOut(BaseModel):
    features: dict[str, bool]
    monthly_token_budget: int | None
    ai_processing_basis: AIProcessingBasis
    tokens_used_this_month: int
    configured: bool = Field(description="Whether an Anthropic API key is configured on the server")
    model: str


class AISettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    features: dict[AIFeature, bool] | None = None
    monthly_token_budget: int | None = Field(default=None, ge=0)
    ai_processing_basis: AIProcessingBasis | None = None


class AIActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    feature: str
    prompt_id: str
    prompt_version: str | None
    model: str | None
    initiator_type: str
    initiator_id: uuid.UUID | None
    subject_type: str | None
    subject_id: uuid.UUID | None
    input_ref: str | None
    status: str
    error_code: str | None
    limitations_note: str | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    consent_basis: str | None
    decision: str | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


# ================================================================ Epic 35 (FR-182): commercial document extraction

CommercialDocKind = Literal["contract", "sow", "po", "rate_card"]
_DECIMAL_RE = r"^-?\d{1,14}(\.\d{1,4})?$"


def _no_float(value: object) -> object:
    """Amounts are decimal strings: pydantic must reject floats (and ints) from the model."""
    if value is None or isinstance(value, str):
        return value
    raise ValueError("amounts must be decimal strings")


class RateCardLineExtraction(BaseModel):
    role: str = Field(max_length=200)
    rate: str = Field(pattern=_DECIMAL_RE)
    rate_unit: Literal["hour", "day", "month"]
    currency: str | None = Field(default=None, max_length=3)

    _rate_str = field_validator("rate", mode="before")(_no_float)


class CommercialExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    doc_kind: CommercialDocKind
    parties: list[str] = Field(default_factory=list, max_length=10)
    effective_from: str | None = None
    effective_to: str | None = None
    value: str | None = Field(default=None, pattern=_DECIMAL_RE)
    currency: str | None = Field(default=None, max_length=3)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    po_number: str | None = Field(default=None, max_length=100)
    po_cap: str | None = Field(default=None, pattern=_DECIMAL_RE)
    rate_card_lines: list[RateCardLineExtraction] = Field(default_factory=list, max_length=200)
    limitations_note: str | None = None

    _amounts_str = field_validator("value", "po_cap", mode="before")(_no_float)


class CommercialExtractIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    doc_kind: CommercialDocKind


class CommercialExtractOut(BaseModel):
    ok: bool
    action_id: uuid.UUID | None
    error_code: str | None = None
    draft: CommercialExtraction | None = None
