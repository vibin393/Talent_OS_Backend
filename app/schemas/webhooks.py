# ruff: file-ignore[line-too-long]
"""Webhook DTOs (Epic 68, spec A3/A4). Secrets are returned ONCE (``SubscriptionCreated`` / ``SecretRotated``); every
other shape carries only ``SigningSecretMeta`` (fingerprint, rotation times)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SubscriptionStatus = Literal["pending_verification", "active", "disabled"]
DeliveryStatus = Literal["pending", "in_flight", "retrying", "succeeded", "failed", "held", "cancelled"]


def _check_filters(value: dict[str, list[str]]) -> dict[str, list[str]]:
    if len(value) > 3:
        msg = "at most 3 filter fields"
        raise ValueError(msg)
    for key, values in value.items():
        if not isinstance(values, list) or not values or len(values) > 50:
            msg = f"filter {key}: 1-50 values"
            raise ValueError(msg)
        for v in values:
            if not isinstance(v, str) or not v or len(v) > 60:
                msg = f"filter {key}: values are strings of 1-60 characters"
                raise ValueError(msg)
    return value


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2000)
    event_types: list[str] = Field(min_length=1, max_length=100)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    description: str | None = Field(default=None, max_length=300)
    max_in_flight: int = Field(default=5, ge=1, le=5)
    suppress_own_changes: bool = False
    installation_id: uuid.UUID | None = None

    _filters = field_validator("filters")(_check_filters)


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=8, max_length=2000)
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=100)
    filters: dict[str, list[str]] | None = None
    description: str | None = Field(default=None, max_length=300)
    max_in_flight: int | None = Field(default=None, ge=1, le=5)
    suppress_own_changes: bool | None = None

    @field_validator("filters")
    @classmethod
    def _f(cls, value: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        return None if value is None else _check_filters(value)


class SigningSecretMeta(BaseModel):
    fingerprint: str | None = None
    rotated_at: datetime | None = None
    previous_expires_at: datetime | None = None


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    owner_kind: Literal["tenant", "installation"]
    installation_id: uuid.UUID | None
    url: str
    url_host: str
    description: str | None
    event_types: list[str]
    all_events: bool
    filters: dict[str, list[str]]
    status: SubscriptionStatus
    disabled_reason: str | None
    paused: bool = False
    verified_at: datetime | None
    verification_error: str | None
    max_in_flight: int
    suppress_own_changes: bool
    consecutive_failures: int
    failure_streak_started_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    disabled_at: datetime | None
    signing_secret: SigningSecretMeta
    created_at: datetime
    updated_at: datetime
    version: str = "0"


class SubscriptionCreated(BaseModel):
    subscription: SubscriptionOut
    signing_secret: str


class SecretRotated(BaseModel):
    subscription: SubscriptionOut
    signing_secret: str
    previous_expires_at: datetime | None


class SubscriptionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubscriptionStatus | None = None
    installation_id: uuid.UUID | None = None
    include_deleted: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=1000)


class EventTypeOut(BaseModel):
    type: str
    version: int
    description: str
    scopes: list[str]
    allowed: bool = True
    requires: list[str] = Field(default_factory=list)
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema", serialization_alias="schema")
    example: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class Attempt(BaseModel):
    n: int
    at: datetime
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    excerpt: str | None = None


class DeliveryOut(BaseModel):
    id: uuid.UUID
    subscription_id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    kind: Literal["event", "replay", "test", "verification"]
    replay_of_id: uuid.UUID | None
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    latency_ms: int | None = None
    delivered_at: datetime | None
    created_at: datetime


class DeliveryDetailOut(DeliveryOut):
    attempts: list[Attempt]
    request_body: dict[str, Any] | None = None


class DeliveryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: list[DeliveryStatus] | None = None
    event_type: str | None = Field(default=None, max_length=60)
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=1000)


class ReplayRangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: datetime = Field(alias="from")
    date_to: datetime = Field(alias="to")
    statuses: list[Literal["failed", "held"]] = Field(default_factory=lambda: ["failed", "held"], min_length=1)
    preview: bool = False


class ReplayRangeOut(BaseModel):
    count: int
    started: bool


class TestEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(max_length=60)


class EchoEndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=80)


class EchoEndpointOut(BaseModel):
    id: uuid.UUID
    label: str | None
    created_at: datetime


class EchoEndpointCreated(EchoEndpointOut):
    url: str


class EchoReceiptOut(BaseModel):
    id: uuid.UUID
    received_at: datetime
    event_type: str | None
    signature_valid: bool
    size: int
    headers: dict[str, Any]
    body: str


__all__ = [
    "Attempt",
    "DeliveryDetailOut",
    "DeliveryOut",
    "DeliveryQuery",
    "DeliveryStatus",
    "EchoEndpointCreate",
    "EchoEndpointCreated",
    "EchoEndpointOut",
    "EchoReceiptOut",
    "EventTypeOut",
    "ReplayRangeIn",
    "ReplayRangeOut",
    "SecretRotated",
    "SigningSecretMeta",
    "SubscriptionCreate",
    "SubscriptionCreated",
    "SubscriptionOut",
    "SubscriptionQuery",
    "SubscriptionStatus",
    "SubscriptionUpdate",
    "TestEventIn",
]
