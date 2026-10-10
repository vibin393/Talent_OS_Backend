"""Phase 2 Epic 33 schemas: reply capture, SMS / WhatsApp channels, opt-outs, manual queue, custom domains."""

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.comms import EmailOut

Channel = Literal["sms", "whatsapp"]
ChannelPurpose = Literal[
    "timesheet_reminder", "onboarding_task", "interview_reminder", "joining_touchpoint", "other_transactional",
    "marketing",  # Epic 39: promotional DLT / WhatsApp templates (DB CHECK widened by Epic 36)
]
TemplateStatus = Literal["draft", "registered", "archived"]
RecipientEntity = Literal["candidate", "contact"]
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DLT_TEMPLATE_ID = re.compile(r"^\d{19}$")
SENDER_ID = re.compile(r"^[A-Za-z0-9]{6}$")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_e164(value: str) -> str:
    value = (value or "").strip().replace(" ", "")
    if not E164.match(value):
        msg = "must be an E.164 phone number (+<country><number>)"
        raise ValueError(msg)
    return value


# ================================================================ reply capture (FR-171)
class ReplyCaptureStatusOut(BaseModel):
    enabled: bool
    provider: str | None
    connection_id: uuid.UUID | None
    scopes_granted: bool
    threads_tracked: int
    last_synced_at: datetime | None
    tenant_enabled: bool


class InboundEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_address: str
    subject: str
    body_text: str
    received_at: datetime
    attachment_names: list[str]


class EmailThreadOut(BaseModel):
    email: EmailOut
    replies: list[InboundEmailOut]


# ================================================================ channel settings
class QuietHours(BaseModel):
    start: str = "21:00"
    end: str = "09:00"
    tz: str = "Asia/Kolkata"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not HHMM.match(value):
            msg = "must be HH:MM"
            raise ValueError(msg)
        return value

    @field_validator("tz")
    @classmethod
    def _tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = "unknown time zone"
            raise ValueError(msg) from exc
        return value


class ChannelSettingsOut(BaseModel):
    sms_enabled: bool
    whatsapp_enabled: bool
    default_sms_sender_id: str | None
    dlt_entity_id: str | None
    quiet_hours: QuietHours
    inbound_sync_enabled: bool
    thread_sync_days: int


class ChannelSettingsUpdate(_In):
    sms_enabled: bool | None = None
    whatsapp_enabled: bool | None = None
    default_sms_sender_id: str | None = Field(default=None, max_length=6)
    dlt_entity_id: str | None = Field(default=None, max_length=30)
    quiet_hours: QuietHours | None = None
    inbound_sync_enabled: bool | None = None
    thread_sync_days: int | None = Field(default=None, ge=7, le=90)

    @field_validator("default_sms_sender_id")
    @classmethod
    def _sender(cls, value: str | None) -> str | None:
        if value and not SENDER_ID.match(value):
            msg = "sender id must be 6 letters / digits"
            raise ValueError(msg)
        return value


# ================================================================ templates
class ChannelTemplateIn(_In):
    channel: Channel
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=200)
    purpose: ChannelPurpose
    body: str = Field(min_length=1, max_length=4000)
    variables: list[str] = Field(default_factory=list, max_length=30)
    provider_template_ref: str | None = Field(default=None, max_length=200)
    language: str = Field(default="en", max_length=10)
    sender_id: str | None = Field(default=None, max_length=6)
    status: TemplateStatus = "draft"


class ChannelTemplateUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: ChannelPurpose | None = None
    body: str | None = Field(default=None, min_length=1, max_length=4000)
    variables: list[str] | None = Field(default=None, max_length=30)
    provider_template_ref: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, max_length=10)
    sender_id: str | None = Field(default=None, max_length=6)
    status: TemplateStatus | None = None


class ChannelTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: Channel
    key: str
    name: str
    purpose: ChannelPurpose
    body: str
    variables: list[str]
    provider_template_ref: str | None
    language: str
    sender_id: str | None
    status: TemplateStatus
    version: int
    sendable: bool = False
    created_at: datetime
    updated_at: datetime


class TemplateSyncOut(BaseModel):
    created: int
    updated: int


# ================================================================ sending
class RecipientRef(_In):
    entity_type: RecipientEntity
    entity_id: uuid.UUID


class ContextRefsIn(_In):
    demand_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    interview_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    link_url: str | None = Field(default=None, max_length=500)


class SendMessagesIn(_In):
    channel: Channel
    template_id: uuid.UUID
    recipients: list[RecipientRef] = Field(min_length=1, max_length=100)
    context_refs: ContextRefsIn | None = None


class SendResultOut(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    status: Literal["queued", "refused"]
    reason: str | None = None
    message_id: uuid.UUID | None = None


class SendMessagesOut(BaseModel):
    results: list[SendResultOut]


class EligibilityIn(_In):
    channel: Channel
    template_id: uuid.UUID | None = None
    recipients: list[RecipientRef] = Field(min_length=1, max_length=100)
    context_refs: ContextRefsIn | None = None


class EligibilityOut(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    eligible: bool
    reason: str | None = None
    preview: str | None = None


class ChannelMessageOut(BaseModel):
    id: uuid.UUID
    channel: Channel
    direction: Literal["outbound", "inbound"]
    template_id: uuid.UUID | None
    template_name: str | None
    status: str
    refusal_reason: str | None
    error_code: str | None
    recipient_kind: str | None
    recipient_id: uuid.UUID | None
    to_masked: str | None
    body_rendered: str
    origin: str
    sent_at: datetime | None
    created_at: datetime
    age_seconds: int


class NoticeBrief(BaseModel):
    id: uuid.UUID
    version: int
    title: str
    body_md: str


class MessagingStateOut(BaseModel):
    has_phone: bool
    phone_masked: str | None
    sms_opted_out: bool
    whatsapp_consent: bool
    whatsapp_notice: NoticeBrief | None


class AttestationIn(_In):
    candidate_id: uuid.UUID
    notice_id: uuid.UUID


# ================================================================ opt-outs / manual queue
class OptOutOut(BaseModel):
    id: uuid.UUID
    channel: Channel
    phone_masked: str | None
    source: str
    recipient_kind: str | None
    recipient_id: uuid.UUID | None
    reason: str | None
    created_at: datetime
    revoked_at: datetime | None


class OptOutIn(_In):
    channel: Channel
    phone_e164: str
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("phone_e164")
    @classmethod
    def _phone(cls, value: str) -> str:
        return check_e164(value)


class RevokeIn(_In):
    reason: str = Field(min_length=1, max_length=500)


class ManualQueueItemOut(BaseModel):
    id: uuid.UUID
    channel: Channel
    to_e164: str | None
    body_rendered: str
    template_name: str | None
    provider_template_ref: str | None
    variables: dict[str, Any]
    created_at: datetime
    age_seconds: int


class MarkFailedIn(_In):
    error_code: str | None = Field(default=None, max_length=64)


# ================================================================ connectors
class MessagingConnectorIn(_In):
    category: Channel
    provider_key: str = Field(min_length=1, max_length=40)
    display_name: str = Field(min_length=1, max_length=200)
    credentials: dict[str, Any] | None = None
    webhook_secret: str | None = Field(default=None, max_length=500)
    settings: dict[str, Any] = Field(default_factory=dict)


class MessagingConnectorUpdate(_In):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    credentials: dict[str, Any] | None = None
    webhook_secret: str | None = Field(default=None, max_length=500)
    settings: dict[str, Any] | None = None
    status: Literal["active", "disabled"] | None = None


class SendTestIn(_In):
    to_e164: str

    @field_validator("to_e164")
    @classmethod
    def _phone(cls, value: str) -> str:
        return check_e164(value)


class StatusOut(BaseModel):
    status: str


# ================================================================ portal / public link
class PortalMessagesOut(BaseModel):
    has_phone: bool
    sms_opted_out: bool
    whatsapp_opted_in: bool
    whatsapp_notice: NoticeBrief | None


class PortalWhatsAppIn(_In):
    enabled: bool
    notice_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _notice(self) -> "PortalWhatsAppIn":
        if self.enabled and self.notice_id is None:
            msg = "notice_id is required to opt in"
            raise ValueError(msg)
        return self


class PortalSmsIn(_In):
    opted_out: bool


# ================================================================ custom domains (FR-174)
class CustomDomainIn(_In):
    host: str = Field(min_length=3, max_length=253)


class CustomDomainOut(BaseModel):
    id: uuid.UUID
    host: str
    status: Literal["pending", "verified", "failed", "removed"]
    txt_name: str
    txt_value: str
    cname_target: str | None
    verified_at: datetime | None
    last_checked_at: datetime | None
    certificate: str = "managed_by_hosting_pending"


class ResolveHostOut(BaseModel):
    slug: str
