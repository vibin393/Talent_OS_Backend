# ruff: file-ignore[line-too-long]
"""Worker "My Work" credential DTOs (Epic 21 A7). No verifier ids, no rejection-note internals, no requirement
ids: the Worker sees their own credentials (last 4 digits only) and the labels of what current Placements need."""

import uuid
from datetime import date

from pydantic import BaseModel

from app.core.field_visibility import register_portal_schema


@register_portal_schema("worker")
class PortalCredentialTypeOut(BaseModel):
    key: str
    label: str
    category: str
    has_expiry: bool
    number_kind: str
    requires_document: bool


@register_portal_schema("worker")
class PortalCredentialOut(BaseModel):
    id: uuid.UUID
    type_key: str
    type_label: str
    number_last4: str | None
    issued_on: date | None
    expires_on: date | None
    status: str  # pending_verification | valid | expiring_soon | expired | rejected
    needs_reupload: bool
    has_document: bool
    version_no: int


@register_portal_schema("worker")
class PortalNeededOut(BaseModel):
    label: str
    kind: str
    type_key: str | None
    status: str


@register_portal_schema("worker")
class PortalCredentialsOut(BaseModel):
    credentials: list[PortalCredentialOut]
    needed: list[PortalNeededOut]
    types: list[PortalCredentialTypeOut]
    expiring_soon: int
