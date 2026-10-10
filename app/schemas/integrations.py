"""Integrations runtime DTOs (Epic 66, spec 0.10)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class LinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    installation_id: uuid.UUID
    entity_type: str
    local_id: uuid.UUID
    external_id: str
    external_url: str | None = None
    local_version: str | None = None
    external_version: str | None = None
    content_sha256: str | None = None
    status: str
    last_synced_at: datetime | None = None
    last_error_code: str | None = None
    candidate_id: uuid.UUID | None = None


class ConnectorListingOut(BaseModel):
    key: str
    name: str
    summary: str
    category: str
    capabilities: list[str]
    auth: str
    version: int
    manifest_sha256: str
    adapters: list[str]
    data_flows: list[dict[str, Any]]
    egress_hosts: list[str]
    secret_fields: list[str]
    config_schema: dict[str, Any]
    installed: bool
    installation_ids: list[uuid.UUID]


class HealthOut(BaseModel):
    installation_id: uuid.UUID
    health_status: str
    error_code: str | None = None
    checked_at: datetime
