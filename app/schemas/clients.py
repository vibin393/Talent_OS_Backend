"""Client and contact API models (Epic 2, stories 2.1 / 2.2)."""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

import phonenumbers
from fastapi import Query
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

ClientStatus = Literal["prospect", "active", "inactive"]
ContactType = Literal["hiring_manager", "hr", "procurement", "finance", "other"]
ClientSort = Literal[
    "name", "-name", "created_at", "-created_at", "updated_at", "-updated_at", "status", "-status"
]

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
WEBSITE_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
DEFAULT_PHONE_REGION = "IN"

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptionalText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


def normalize_gstin(value: str | None) -> str | None:
    if value is None:
        return None
    gstin = value.strip().upper()
    if not GSTIN_RE.fullmatch(gstin):
        msg = "GSTIN must look like 22AAAAA0000A1Z5"
        raise ValueError(msg)
    return gstin


def normalize_website(value: str | None) -> str | None:
    if value is None:
        return None
    site = value.strip()
    if "://" not in site:
        site = f"https://{site}"
    if not WEBSITE_RE.fullmatch(site):
        msg = "Website must be a valid http(s) URL"
        raise ValueError(msg)
    return site


def normalize_phone(value: str | None, region: str = DEFAULT_PHONE_REGION) -> str | None:
    """E.164, default region IN (AD-14)."""
    if value is None:
        return None
    try:
        parsed = phonenumbers.parse(value.strip(), region)
    except phonenumbers.NumberParseException as exc:
        msg = "Phone number is not valid"
        raise ValueError(msg) from exc
    if not phonenumbers.is_valid_number(parsed):
        msg = "Phone number is not valid"
        raise ValueError(msg)
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class Location(BaseModel):
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    state: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    country: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class _ClientFields(BaseModel):
    industry: OptionalText | None = None
    website: Annotated[str, StringConstraints(max_length=500)] | None = None
    locations: Annotated[list[Location], Field(max_length=50)] | None = None
    gstin: str | None = None

    @field_validator("industry", "website", "gstin", mode="before")
    @classmethod
    def _blank(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, value: str | None) -> str | None:
        return normalize_gstin(value)

    @field_validator("website")
    @classmethod
    def _website(cls, value: str | None) -> str | None:
        return normalize_website(value)


class ClientCreate(_ClientFields):
    name: Name
    owner_id: uuid.UUID | None = None  # default: the caller
    status: ClientStatus = "prospect"


class ClientUpdate(_ClientFields):
    name: Name | None = None
    owner_id: uuid.UUID | None = None
    status: ClientStatus | None = None


class OwnerRef(BaseModel):
    id: uuid.UUID
    name: str


class ClientOut(BaseModel):
    id: uuid.UUID
    name: str
    industry: str | None
    website: str | None
    locations: list[Location]
    gstin: str | None
    owner: OwnerRef | None
    owner_id: uuid.UUID
    status: ClientStatus
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SimilarClient(BaseModel):
    id: uuid.UUID
    name: str
    similarity: float


class ClientCreatedOut(ClientOut):
    # Existing clients with a similar name (pg_trgm similarity >= 0.6); informational only.
    warnings: list[SimilarClient] = Field(default_factory=list)


@dataclass(frozen=True)
class ClientFilters:
    """``Annotated[ClientFilters, Depends()]``."""

    q: Annotated[str | None, Query(max_length=200)] = None
    status: Annotated[ClientStatus | None, Query()] = None
    owner_id: Annotated[uuid.UUID | None, Query()] = None
    include_archived: Annotated[bool, Query()] = False
    sort: Annotated[ClientSort, Query()] = "name"


# ---------------------------------------------------------------- contacts
class _ContactFields(BaseModel):
    title: OptionalText | None = None
    email: Annotated[EmailStr, StringConstraints(max_length=254)] | None = None
    phone: Annotated[str, StringConstraints(max_length=40)] | None = None

    @field_validator("title", "email", "phone", mode="before")
    @classmethod
    def _blank(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("email")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value)


class ContactCreate(_ContactFields):
    name: Name
    type: ContactType = "other"


class ContactUpdate(_ContactFields):
    name: Name | None = None
    type: ContactType | None = None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    title: str | None
    email: str | None
    phone: str | None
    type: ContactType
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class Client360Counts(BaseModel):
    contacts: int
    activities: int
    demands: int = 0
    open_demands: int = 0
    submissions: int = 0


class Client360Out(BaseModel):
    client: ClientOut
    contacts: list[ContactOut]
    counts: Client360Counts
    # Placeholders until Epic 4 (demands) and Epic 6 (submissions) land; keys are always present.
    demands: list[dict[str, object]] = Field(default_factory=list)
    submissions: list[dict[str, object]] = Field(default_factory=list)


class QuickSearchHit(BaseModel):
    id: uuid.UUID
    kind: Literal["client", "contact"]
    title: str
    subtitle: str | None = None
    client_id: uuid.UUID | None = None
