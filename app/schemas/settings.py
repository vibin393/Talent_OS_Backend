"""Tenant settings hub models (Epic 12 story 12.8, FR-74). Range/format rules are enforced in
``settings_service`` so errors come back as 422 ``validation_failed`` with ``details.fields{path: message}``."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SettingsCatalogEntry(BaseModel):
    category: str
    key: str
    label: str
    description: str
    endpoint: str
    available: bool
    summary: str | None = None
    # Epic 35 (A9): the permission the endpoint requires and the owning epic of registry entries.
    permission: str | None = None
    owner_epic: str | None = None


class SettingsOut(BaseModel):
    catalog: list[SettingsCatalogEntry]
    company: dict[str, Any]
    regional: dict[str, Any]
    id_prefixes: dict[str, str]
    link_expiries: dict[str, int]


class GrievanceOfficer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)


class CompanyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)
    legal_name: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=1000)
    website: str | None = Field(default=None, max_length=300)
    logo_file_id: uuid.UUID | None = None
    grievance_officer: GrievanceOfficer | None = None


class RegionalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = Field(default=None, max_length=64)
    currency: str | None = Field(default=None, max_length=3)
    date_format: str | None = Field(default=None, max_length=20)
    number_format: str | None = Field(default=None, max_length=20)


class IdPrefixesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    REQ: str | None = None
    CAN: str | None = None
    APP: str | None = None
    SUB: str | None = None
    DSR: str | None = None


class LinkExpiriesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consent_request: int | None = None
    ai_screening: int | None = None
    client_feedback: int | None = None
    dsr_export: int | None = None
