"""Documents, templates and eSignature (Epic 22, AD-28). Migrations ``20261003_209_01..03`` (branch
``p2_documents``).

* ``document_templates`` / ``document_template_versions`` -- versioned Markdown templates with a strict merge
  catalogue (internal-only org RLS).
* ``documents`` / ``document_versions`` -- the repository; bytes live in ``stored_files``. Versions are
  append-only; a signed document only allows ``archived_at`` / ``superseded_by_id`` / ``expires_on``.
* ``signature_requests`` / ``signers`` / ``signatures`` / ``signature_events`` / ``signer_otps`` -- ordered
  multi-party typed-name signing with email OTP and a certificate (evidence is immutable once completed).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

TEMPLATE_KINDS = (
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
)
TEMPLATE_VERSION_STATUSES = ("draft", "published", "retired")
ACCESS_LEVELS = ("entity", "hr", "finance")
VERSION_SOURCES = ("upload", "generated", "signed", "migrated")
REQUEST_STATUSES = ("draft", "sent", "partially_signed", "completed", "declined", "expired", "cancelled")
OPEN_REQUEST_STATUSES = ("sent", "partially_signed")
SIGNER_KINDS = ("candidate", "user", "client_user", "contact", "vendor_user")
SIGNER_STATUSES = ("pending", "notified", "signed", "declined", "void")
CHANNELS = ("portal", "session", "secure_link")
EVENT_KINDS = (
    "created",
    "sent",
    "viewed",
    "otp_sent",
    "otp_failed",
    "consented",
    "signed",
    "declined",
    "reminded",
    "expired",
    "cancelled",
    "completed",
    "verified",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tfk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ templates
class DocumentTemplate(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "document_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("kind", TEMPLATE_KINDS), name="kind"),
        Index("ix_document_templates_kind", "kind", "legal_entity_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signer_roles: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    esign_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DocumentTemplateVersion(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "document_template_versions"
    __audit_exclude__ = ("body_md",)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("template_id", "version_no"),
        CheckConstraint(_in("status", TEMPLATE_VERSION_STATUSES), name="status"),
        _tfk("template_id", "document_templates", "fk_document_template_versions_template_id", "CASCADE"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    merge_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ repository
class Document(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "documents"
    __audit_exclude__ = ("title", "note")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("access_level", ACCESS_LEVELS), name="access_level"),
        Index("ix_documents_owner", "owner_kind", "owner_id"),
        Index("ix_documents_worker_candidate_id", "worker_candidate_id"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    worker_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    access_level: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'entity'"))
    retention_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P1 attachments facade (``attachments.note``); erased with the document.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DocumentVersion(UUIDPk, TenantMixin, Base):
    """Append-only (immutability trigger); never audited per row (the document is)."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("document_id", "version_no"),
        CheckConstraint(_in("source", VERSION_SOURCES), name="source"),
        _tfk("document_id", "documents", "fk_document_versions_document_id"),
        _tfk("file_id", "stored_files", "fk_document_versions_file_id"),
        Index("ix_document_versions_sha256", "sha256"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    worker_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signature_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'system'"))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ================================================================ eSignature
class SignatureRequest(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "signature_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="status"),
        _tfk("document_id", "documents", "fk_signature_requests_document_id"),
        _tfk("document_version_id", "document_versions", "fk_signature_requests_document_version_id"),
        Index("ix_signature_requests_subject", "subject_kind", "subject_id"),
        Index("ix_signature_requests_status", "status", "expires_at"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    doc_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'builtin'"))
    provider_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    require_otp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reminder_every_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    final_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    certificate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Signer(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "signers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("request_id", "order"),
        CheckConstraint(_in("kind", SIGNER_KINDS), name="kind"),
        CheckConstraint(_in("status", SIGNER_STATUSES), name="status"),
        _tfk("request_id", "signature_requests", "fk_signers_request_id"),
        Index("ix_signers_principal", "kind", "principal_id"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Signature(UUIDPk, TenantMixin, Base):
    __tablename__ = "signatures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("signer_id"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        _tfk("signer_id", "signers", "fk_signatures_signer_id"),
        _tfk("request_id", "signature_requests", "fk_signatures_request_id"),
    )

    signer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    typed_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    doc_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(12), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'builtin'"))


class SignatureEvent(UUIDPk, TenantMixin, Base):
    __tablename__ = "signature_events"
    __table_args__ = (
        CheckConstraint(_in("kind", EVENT_KINDS), name="kind"),
        _tfk("request_id", "signature_requests", "fk_signature_events_request_id"),
        Index("ix_signature_events_request", "request_id", "at"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    signer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class SignerOtp(UUIDPk, TenantMixin, Base):
    __tablename__ = "signer_otps"
    __table_args__ = (
        _tfk("signer_id", "signers", "fk_signer_otps_signer_id", "CASCADE"),
        Index("ix_signer_otps_signer", "signer_id", "created_at"),
    )

    signer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EsignCertificateLookup(Base):
    """Platform table: certificate id -> tenant + request (no PII) for the public ``/verify?c=`` page."""

    __tablename__ = "esign_certificate_lookups"

    certificate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
