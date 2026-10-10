"""Documents / eSignature contract DTOs (Epic 13 contract layer; filled by Epic 22)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class RenderedFile(_Frozen):
    file_id: uuid.UUID
    sha256: str
    page_count: int


class SignerSpec(_Frozen):
    kind: str
    ref_id: uuid.UUID | None
    email: str
    order: int
    name: str | None = None


class SignatureRequestCreateIn(_Frozen):
    """Either ``document_id`` or ``template_id`` (+ ``merge_data`` / ``merge_refs``)."""

    document_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    merge_data: dict[str, Any] | None = None
    merge_refs: dict[str, uuid.UUID] | None = None
    subject_kind: str
    subject_id: uuid.UUID
    owner_kind: str | None = None
    owner_id: uuid.UUID | None = None
    signers: list[SignerSpec]
    expires_at: datetime | None = None
    send: bool = True

    @model_validator(mode="after")
    def _source(self) -> "SignatureRequestCreateIn":
        if (self.document_id is None) == (self.template_id is None):
            msg = "give either document_id or template_id"
            raise ValueError(msg)
        return self


class SignatureRequestSummary(_Frozen):
    id: uuid.UUID
    status: str
    subject_kind: str
    subject_id: uuid.UUID
    doc_sha256: str | None
    final_sha256: str | None


# ====================================================================== Epic 22 API schemas
# (The four contract DTOs above are frozen by ``tests/contracts/signatures.json``.)
from datetime import date  # noqa: E402
from typing import Literal  # noqa: E402

from pydantic import ConfigDict as _ConfigDict  # noqa: E402
from pydantic import Field  # noqa: E402

TemplateKind = Literal[
    "employment_agreement",
    "contractor_agreement",
    "nda",
    "policy",
    "offer_letter",
    "extension_letter",
    "relieving_letter",
    "experience_letter",
    "client_form",
    "other",
]
AccessLevel = Literal["entity", "hr", "finance"]
SignerRoleKind = Literal["worker", "tenant_signatory", "client", "vendor"]
SignerKind = Literal["candidate", "user", "client_user", "contact", "vendor_user"]


class _In(BaseModel):
    model_config = _ConfigDict(extra="forbid")


class SignerRole(_In):
    role: SignerRoleKind
    order: int = Field(ge=1, le=20)
    label: str = Field(min_length=1, max_length=100)


class TemplateCreateIn(_In):
    name: str = Field(min_length=1, max_length=200)
    kind: TemplateKind
    legal_entity_id: uuid.UUID | None = None
    signer_roles: list[SignerRole] = Field(default_factory=list, max_length=10)
    esign_config_id: uuid.UUID | None = None
    body_md: str = Field(default="", max_length=200_000)


class TemplatePatchIn(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_entity_id: uuid.UUID | None = None
    signer_roles: list[SignerRole] | None = Field(default=None, max_length=10)
    esign_config_id: uuid.UUID | None = None
    archived: bool | None = None


class TemplateVersionIn(_In):
    body_md: str = Field(min_length=1, max_length=200_000)


class TemplateVersionOut(BaseModel):
    id: uuid.UUID
    template_id: uuid.UUID
    version_no: int
    body_md: str
    merge_fields: list[str]
    status: str
    published_at: datetime | None
    created_at: datetime


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    legal_entity_id: uuid.UUID | None
    signer_roles: list[dict[str, Any]]
    esign_config_id: uuid.UUID | None
    current_version_id: uuid.UUID | None
    current_version_no: int | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    versions: list[TemplateVersionOut] = Field(default_factory=list)


class MergeCatalogueOut(BaseModel):
    namespaces: dict[str, list[str]]
    filters: list[str]


class DocumentVersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    sha256: str
    size: int
    source: str
    file_name: str | None
    content_type: str | None
    template_version_id: uuid.UUID | None
    signature_request_id: uuid.UUID | None
    created_by_kind: str
    created_by_id: uuid.UUID | None
    created_at: datetime
    download_url: str


class DocumentOut(BaseModel):
    id: uuid.UUID
    human_id: str
    owner_kind: str
    owner_id: uuid.UUID
    category: str
    title: str
    access_level: str
    retention_class: str | None
    expires_on: date | None
    signed: bool
    superseded_by_id: uuid.UUID | None
    archived_at: datetime | None
    note: str | None
    current_version: DocumentVersionOut | None
    versions: list[DocumentVersionOut] | None = None
    created_at: datetime
    updated_at: datetime


class DocumentPatchIn(_In):
    category: str | None = Field(default=None, min_length=1, max_length=40)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    expires_on: date | None = None
    clear_expiry: bool = False
    access_level: AccessLevel | None = None


class SignerIn(_In):
    kind: SignerKind
    principal_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    email: str | None = Field(default=None, max_length=320)
    name: str | None = Field(default=None, max_length=300)


class OwnerRef(_In):
    kind: str = Field(min_length=1, max_length=40)
    id: uuid.UUID


class SignatureRequestIn(_In):
    template_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    merge_refs: dict[str, uuid.UUID] = Field(default_factory=dict)
    merge_data: dict[str, Any] | None = None
    owner: OwnerRef
    subject: OwnerRef | None = None
    title: str | None = Field(default=None, max_length=300)
    signers: list[SignerIn] = Field(min_length=1, max_length=10)
    expires_in_days: int = Field(default=14, ge=1, le=90)
    send: bool = True
    legal_entity_id: uuid.UUID | None = None


class CancelIn(_In):
    reason: str = Field(min_length=3, max_length=2000)


class SignerOut(BaseModel):
    id: uuid.UUID
    order: int
    kind: str
    principal_id: uuid.UUID | None
    name: str
    email: str
    status: str
    notified_at: datetime | None
    signed_at: datetime | None = None


class SignatureEventOut(BaseModel):
    kind: str
    signer_id: uuid.UUID | None
    at: datetime
    data: dict[str, Any]


class SignatureRequestOut(BaseModel):
    id: uuid.UUID
    human_id: str
    title: str
    status: str
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    provider: str
    doc_sha256: str | None
    final_sha256: str | None
    final_version_id: uuid.UUID | None
    certificate_id: uuid.UUID
    require_otp: bool
    expires_at: datetime
    sent_at: datetime | None
    completed_at: datetime | None
    cancel_reason: str | None
    created_at: datetime
    signers: list[SignerOut]
    events: list[SignatureEventOut] = Field(default_factory=list)


# ---------------------------------------------------------------- signing ceremony
class CeremonyOut(BaseModel):
    """What a signer sees: never other signers' emails."""

    request_id: uuid.UUID
    signer_id: uuid.UUID
    human_id: str
    title: str
    tenant_name: str | None
    status: str
    signer_name: str
    signer_status: str
    your_turn: bool
    require_otp: bool
    consented: bool
    otp_verified: bool
    expires_at: datetime
    doc_sha256: str | None
    final_sha256: str | None
    signers: list[dict[str, Any]]
    document_url: str


class ConsentIn(_In):
    agreed: bool


class SignIn(_In):
    typed_name: str = Field(min_length=1, max_length=300)
    otp: str | None = Field(default=None, max_length=12)
    agreed: bool = True


class DeclineSignIn(_In):
    reason: str | None = Field(default=None, max_length=2000)


class OtpSentOut(BaseModel):
    sent: bool
    expires_at: datetime
    email_hint: str


class CeremonyActionOut(BaseModel):
    status: str
    request_status: str


# ---------------------------------------------------------------- verification
class VerifySigner(BaseModel):
    name: str
    signed_at: datetime | None


class VerifyOut(BaseModel):
    match: Literal["final", "original", "none"]
    request_id: uuid.UUID | None = None
    human_id: str | None = None
    status: str | None = None
    completed_at: datetime | None = None
    signers: list[VerifySigner] = Field(default_factory=list)


class PublicVerifyOut(BaseModel):
    valid: bool
    title: str | None = None
    completed_at: datetime | None = None
    signer_count: int = 0
