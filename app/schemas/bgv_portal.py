"""Worker ("My Work") and Client Portal BGV DTOs (Epic 20 A4/A6/A7).

Workers see their cases, consent notice, document checklist, discrepancy descriptions and reference-check
referees -- never internal notes, verifier details, adjudication reasons, provider names (unless named in the
consent notice) or reference answers. Client Users see only ``{status: clear|not_cleared|null}``.
"""

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.core.field_visibility import register_portal_schema

Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_portal_schema("worker")
class PortalDocumentOut(BaseModel):
    key: str
    label: str
    kind: str
    required: bool
    status: str
    file_count: int
    declared: dict[str, Any] | None
    return_note: str | None


@register_portal_schema("worker")
class PortalCaseOut(BaseModel):
    id: uuid.UUID
    human_id: str
    status: Literal["in_progress", "clear", "failed"]
    stage: str
    consent_given: bool
    check_types: list[str]
    documents: list[PortalDocumentOut]
    documents_submitted: bool
    created_at: datetime


@register_portal_schema("worker")
class PortalConsentView(BaseModel):
    case_id: uuid.UUID
    notice_id: uuid.UUID
    title: str
    body_md: str
    version: int
    check_types: list[str]
    providers: list[str]
    consent_given: bool


@register_portal_schema("worker")
class PortalDiscrepancyOut(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    check_type: str
    severity: str
    description: str
    status: str
    response_due_on: date
    candidate_response: str | None
    responded_at: datetime | None


@register_portal_schema("worker")
class PortalRefereeOut(BaseModel):
    id: uuid.UUID
    name: str
    relationship: str | None
    company: str | None
    status: str


@register_portal_schema("worker")
class PortalReferenceCheckOut(BaseModel):
    id: uuid.UUID
    status: str
    min_referees: int
    consent_given: bool
    notice_id: uuid.UUID | None
    notice_title: str | None
    notice_body_md: str | None
    referees: list[PortalRefereeOut]
    created_at: datetime


@register_portal_schema("worker")
class PortalActionOut(BaseModel):
    status: str


@register_portal_schema("client_user")
class ClientBgvStatusOut(BaseModel):
    status: Literal["clear", "not_cleared"] | None


class PortalConsentIn(_In):
    agreed: bool
    notice_id: uuid.UUID


class EmploymentEntry(_In):
    employer: Short
    designation: Short | None = None
    from_date: date
    to_date: date | None = None
    hr_contact_name: Short | None = None
    hr_contact_email: EmailStr | None = None
    hr_contact_phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None


class EducationEntry(_In):
    institution: Short
    qualification: Short
    year: int = Field(ge=1950, le=2100)


class AddressEntry(_In):
    line: Short
    city: Short
    state: Short | None = None
    pincode: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{6}$")] | None = None
    from_date: date | None = None
    to_date: date | None = None


class EmploymentDeclaration(_In):
    entries: list[EmploymentEntry] = Field(min_length=1, max_length=15)


class EducationDeclaration(_In):
    entries: list[EducationEntry] = Field(min_length=1, max_length=15)


class AddressDeclaration(_In):
    entries: list[AddressEntry] = Field(min_length=1, max_length=15)


class DeclarationIn(_In):
    """Structured declaration of one request key (validated per key by the service)."""

    data: dict[str, Any]


class RespondIn(_In):
    response: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=4000)]


class RefereeIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]
    relationship: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    company: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    email: EmailStr
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None

    @field_validator("phone")
    @classmethod
    def _e164(cls, v: str | None) -> str | None:
        return normalise_phone(v)


class RefereesIn(_In):
    referees: list[RefereeIn] = Field(min_length=1, max_length=10)
    consent_notice_id: uuid.UUID | None = None
    agreed: bool = False


def normalise_phone(value: str | None) -> str | None:
    """E.164 (India default ``+91`` for 10-digit numbers)."""
    if value is None or not value.strip():
        return None
    digits = re.sub(r"[\s\-().]", "", value)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        if len(digits) == 10 and digits[0] in "6789":
            digits = "+91" + digits
        elif len(digits) == 12 and digits.startswith("91"):
            digits = "+" + digits
        else:
            msg = "Enter the phone number with its country code"
            raise ValueError(msg)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", digits):
        msg = "Enter a valid phone number"
        raise ValueError(msg)
    return digits
