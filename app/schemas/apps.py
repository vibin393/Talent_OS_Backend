"""Internal App management DTOs (Epic 67, spec A6): private Apps, secrets, Connected apps."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.oauth import AppOut


class AppSecretOut(BaseModel):
    id: uuid.UUID
    prefix: str
    fingerprint: str
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class AppDetailOut(AppOut):
    secrets: list[AppSecretOut] = Field(default_factory=list)
    installation_id: uuid.UUID | None = None


class RevokeSecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_last: bool = False


class RetireAppIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class ScopeCatalogueOut(BaseModel):
    key: str
    label: str
    description: str
    sensitive: bool
    resources: list[str]
    api_key_allowed: bool


class ConnectedAppOut(BaseModel):
    id: uuid.UUID
    installation_id: uuid.UUID
    app_id: uuid.UUID | None
    app_name: str
    publisher_kind: str
    publisher_name: str
    resource: str
    scopes: list[str]
    scope_labels: list[str]
    granted_at: datetime
    last_used_at: datetime | None = None
