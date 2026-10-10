"""Provider connector configs and jobs (Epic 13, AD-29). Credentials and webhook secrets are write-only."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Category = Literal["esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp", "assessment"]  # Epic 40: +assessment
# Epic 66: every stored category (``models.connectors.CONNECTOR_CATEGORIES``) -- outputs and the webhook path; inputs keep
# ``Category`` (Installations create the Phase 3-5 category configs through the marketplace).
AnyCategory = Literal[
    "esign", "bgv", "einvoice", "payroll", "accounting", "sms", "whatsapp", "assessment", "dsc", "tds_statement", "fx",
    "voice", "job_board", "hrms", "stt",
]  # fmt: skip
ConfigStatus = Literal["active", "disabled"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "dead"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectorConfigIn(_In):
    category: Category
    provider_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    legal_entity_id: uuid.UUID | None = None
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    credentials: dict[str, Any] | None = None
    webhook_secret: Annotated[str, StringConstraints(min_length=8, max_length=500)] | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class ConnectorConfigUpdate(_In):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    credentials: dict[str, Any] | None = None
    webhook_secret: Annotated[str, StringConstraints(min_length=8, max_length=500)] | None = None
    settings: dict[str, Any] | None = None
    status: ConfigStatus | None = None


class ConnectorConfigOut(BaseModel):
    """Never carries credentials or the webhook secret -- only whether they are set."""

    id: uuid.UUID
    category: AnyCategory
    provider_key: str
    legal_entity_id: uuid.UUID | None
    display_name: str
    settings: dict[str, Any]
    status: ConfigStatus
    has_credentials: bool
    has_webhook_secret: bool
    webhook_path: str
    created_at: datetime


class ConnectorJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: AnyCategory
    op: str
    subject_kind: str
    subject_id: uuid.UUID
    version: int
    idempotency_key: str
    config_id: uuid.UUID | None
    status: JobStatus
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    age_seconds: int = 0
