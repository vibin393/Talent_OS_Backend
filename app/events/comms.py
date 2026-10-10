"""Communication events (Epic 10, dispatched after commit). Ids and codes only -- never addresses or bodies."""  # noqa: E501

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class EmailQueued:
    """Messages were committed as ``queued``; the handler enqueues ``comms.send_email`` for each."""

    tenant_id: uuid.UUID
    message_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class EmailSent:
    tenant_id: uuid.UUID
    message_id: uuid.UUID
    sender_user_id: uuid.UUID | None
    transport: str
    origin: str


@dataclasses.dataclass(frozen=True, slots=True)
class EmailFailed:
    tenant_id: uuid.UUID
    message_id: uuid.UUID
    sender_user_id: uuid.UUID | None
    error_code: str | None
    origin: str


@dataclasses.dataclass(frozen=True, slots=True)
class EmailBounced:
    tenant_id: uuid.UUID
    message_id: uuid.UUID | None
    recipient_id: uuid.UUID | None
    sender_user_id: uuid.UUID | None
    bounce_type: str
    entity_type: str | None
    entity_id: uuid.UUID | None


# ================================================================ Phase 2 Epic 33 (FR-171..FR-173)
@dataclasses.dataclass(frozen=True, slots=True)
class EmailReplyReceived:
    """A reply to a Talent OS-originated thread was captured (``comms.email_reply_received``)."""

    tenant_id: uuid.UUID
    inbound_email_id: uuid.UUID
    email_message_id: uuid.UUID
    thread_id: uuid.UUID
    sender_user_id: uuid.UUID | None
    candidate_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ChannelMessageQueued:
    tenant_id: uuid.UUID
    message_id: uuid.UUID
    channel: str
    origin: str


@dataclasses.dataclass(frozen=True, slots=True)
class ChannelMessageStatusChanged:
    tenant_id: uuid.UUID
    message_id: uuid.UUID
    channel: str
    status: str
    error_code: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ChannelInboundReceived:
    """An inbound SMS / WhatsApp message was stored (``comms.channel_inbound_received``)."""

    tenant_id: uuid.UUID
    message_id: uuid.UUID
    channel: str
    matched: bool
    candidate_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ChannelOptOutRecorded:
    tenant_id: uuid.UUID
    opt_out_id: uuid.UUID
    channel: str
    source: str
    candidate_id: uuid.UUID | None = None
