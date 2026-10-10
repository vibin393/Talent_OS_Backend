"""API key models (Epic 12 story 12.7, FR-75)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=200)
    rate_limit_per_min: int = Field(default=600, ge=60, le=6000)
    expires_at: datetime | None = None


class ApiKeyRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grace_hours: int = Field(default=24, ge=0, le=72)


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    rate_limit_per_min: int
    expires_at: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime
    last_used_at: datetime | None
    last_used_ip: str | None
    revoked_at: datetime | None
    revoked_by: uuid.UUID | None
    rotated_from_id: uuid.UUID | None
    status: str = "active"


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full key. Shown once; only its SHA-256 is stored.")


class ApiKeyUsageRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    at: datetime
    method: str
    route_template: str
    status: int
    ip: str | None
    request_id: str | None


class ApiKeyUsageOut(BaseModel):
    total_last_24h: int
    by_status: dict[str, int]
    recent: list[ApiKeyUsageRow]
