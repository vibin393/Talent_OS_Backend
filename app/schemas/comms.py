"""Communication API models (Epic 10, story 10.1): templates, preview, sending, email log, bounces."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

TemplateKey = Literal[
    "submission",
    "interview_invite",
    "interview_reminder",
    "rejection",
    "offer",
    "application_confirmation",
    "ai_screening_invite",
    "client_feedback_request",
    "custom",
]
Category = Literal["process", "marketing"]
RecipientEntity = Literal["candidate", "contact"]
MAX_BODY = 200_000

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
BodyHtml = Annotated[str, StringConstraints(max_length=MAX_BODY)]
Language = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=5)]


# ================================================================ templates
class TemplateCreate(BaseModel):
    key: TemplateKey
    name: Name
    description: Annotated[str, StringConstraints(max_length=1000)] | None = None
    language: Language = "en"
    subject: Subject
    body_html: BodyHtml


class TemplateUpdate(BaseModel):
    name: Name | None = None
    description: Annotated[str, StringConstraints(max_length=1000)] | None = None


class VariantIn(BaseModel):
    subject: Subject
    body_html: BodyHtml


class VariantOut(BaseModel):
    id: uuid.UUID
    language: str
    subject: str
    body_html: str
    version: int
    updated_at: datetime


class TemplateOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    is_system: bool
    languages: list[str]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TemplateDetailOut(TemplateOut):
    variants: list[VariantOut]


class MergeFieldOut(BaseModel):
    path: str
    token: str
    label: str
    sample: str


class MergeFieldGroupOut(BaseModel):
    group: str
    label: str
    fields: list[MergeFieldOut]


class ContextRefs(BaseModel):
    """Entities whose values fill the merge fields (each must be readable by the caller)."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    demand_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    interview_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    link_url: Annotated[str, StringConstraints(max_length=2000)] | None = None


class PreviewIn(BaseModel):
    key: TemplateKey = "custom"
    subject: Annotated[str, StringConstraints(max_length=500)] = ""
    body_html: BodyHtml = ""
    context_refs: ContextRefs | None = None


class PreviewOut(BaseModel):
    subject: str
    body_html: str
    render_warnings: list[str]
    used_samples: bool


class RenderedTemplate(BaseModel):
    template_id: uuid.UUID
    variant_id: uuid.UUID
    version: int
    language: str
    subject: str
    body_html: str
    render_warnings: list[str]


# ================================================================ sending
class RecipientIn(BaseModel):
    """Either an entity (candidate / contact, its email is used) or a bare address."""

    model_config = ConfigDict(extra="forbid")

    entity_type: RecipientEntity | None = None
    entity_id: uuid.UUID | None = None
    address: EmailStr | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "RecipientIn":
        has_entity = self.entity_type is not None and self.entity_id is not None
        if has_entity == (self.address is not None) or (self.entity_type is None) != (self.entity_id is None):
            msg = "give either entity_type + entity_id or address"
            raise ValueError(msg)
        return self


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Annotated[str, StringConstraints(pattern=r"^[a-z_]{2,32}$")]
    entity_id: uuid.UUID


class EmailSendIn(BaseModel):
    to: Annotated[list[RecipientIn], Field(min_length=1, max_length=50)]
    cc: Annotated[list[RecipientIn], Field(max_length=20)] = Field(default_factory=list)
    template_variant_id: uuid.UUID | None = None
    subject: Subject
    body_html: BodyHtml
    category: Category = "process"
    related: Annotated[list[EntityRef], Field(max_length=10)] = Field(default_factory=list)
    context_refs: ContextRefs = Field(default_factory=ContextRefs)


class SendWarnings(BaseModel):
    bounced_addresses: list[str] = Field(default_factory=list)
    suppressed_addresses: list[str] = Field(default_factory=list)
    render_warnings: list[str] = Field(default_factory=list)


class EmailSendOut(BaseModel):
    message_ids: list[uuid.UUID]
    warnings: SendWarnings


class AddressStatusOut(BaseModel):
    address: str
    hard_bounced_at: datetime | None
    last_bounce_reason: str | None
    marketing_opt_out: bool


class RecipientOut(BaseModel):
    id: uuid.UUID
    kind: str
    address: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    status: str
    bounced_at: datetime | None
    bounce_type: str | None


class EmailOut(BaseModel):
    id: uuid.UUID
    sender_user_id: uuid.UUID | None
    sender_name: str | None
    transport: str
    fallback_reason: str | None
    from_address: str
    reply_to: str | None
    subject: str
    status: str
    category: str
    origin: str
    error_code: str | None
    attempts: int
    created_at: datetime
    sent_at: datetime | None
    related: list[dict[str, Any]]
    recipients: list[RecipientOut]


class EmailDetailOut(EmailOut):
    body_html: str


# ================================================================ bounces
class BounceIn(BaseModel):
    """Generic bounce webhook body (``X-TOS-Signature: sha256=<hex HMAC of the raw body>``)."""

    tenant_id: uuid.UUID
    address: EmailStr
    bounce_type: Literal["hard", "soft", "complaint"] = "hard"
    reason: Annotated[str, StringConstraints(max_length=500)] | None = None
    message_id: uuid.UUID | None = None  # our X-TOS-Message-Id
    provider_message_id: Annotated[str, StringConstraints(max_length=255)] | None = None


class BounceOut(BaseModel):
    matched: bool
    recipient_ids: list[uuid.UUID]
