"""DSC-signed invoice / credit-note PDFs (Epic 51, FR-253)."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

DscKind = Literal["invoice", "credit_note"]
DscStatus = Literal["not_required", "pending", "signed", "failed"]

_Str200 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
_Str100 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DscPolicyIn(_In):
    gst_registration_id: uuid.UUID
    client_id: uuid.UUID | None = None
    required: bool = True
    provider_config_id: uuid.UUID | None = None
    signer_ref: _Str200 | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] = (
        "Invoice issued"
    )
    location: _Str100 | None = None
    active: bool = True


class DscPolicyPatch(_In):
    required: bool | None = None
    provider_config_id: uuid.UUID | None = None
    signer_ref: _Str200 | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = (
        None
    )
    location: _Str100 | None = None
    active: bool | None = None


class DscPolicyOut(_Out):
    id: uuid.UUID
    gst_registration_id: uuid.UUID
    client_id: uuid.UUID | None
    required: bool
    provider_config_id: uuid.UUID | None
    signer_ref: str | None
    reason: str
    location: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class DscStatusOut(BaseModel):
    kind: DscKind
    doc_id: uuid.UUID
    dsc_status: DscStatus
    required: bool
    request_status: Literal["pending", "signed", "failed"] | None = None
    adapter_key: str | None = None
    attempts: int = 0
    last_error: str | None = None
    requested_at: datetime | None = None
    signed_at: datetime | None = None
    signer_subject: str | None = None
    signer_serial: str | None = None
    signing_time: datetime | None = None
    signed_pdf_file_id: uuid.UUID | None = None
    render_sha256: str | None = None


class DscVerificationOut(BaseModel):
    intact: bool
    valid: bool
    trusted: Literal["unknown"] = "unknown"
    sub_filter: str | None = None
    signer_subject: str | None = None
    signer_serial: str | None = None
    signing_time: datetime | None = None
    warnings: list[str] = []


class DscUploadOut(BaseModel):
    status: DscStatusOut
    verification: DscVerificationOut


class DscPendingOut(BaseModel):
    request_id: uuid.UUID
    kind: DscKind
    doc_id: uuid.UUID
    number: str | None
    client_id: uuid.UUID
    client_name: str | None
    status: Literal["pending", "failed"]
    age_hours: float
    adapter_key: str
    attempts: int
    last_error: str | None
    requested_at: datetime
