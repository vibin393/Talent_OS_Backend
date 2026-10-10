"""Candidate database tables (Epic 3).

* Platform tables (no RLS, SELECT-only for ``talent_app``; see ``app.core.tenancy.PLATFORM_TABLES``):
  ``skills``, ``skill_aliases`` -- the seeded global skill catalog.
* Everything else is tenant-owned with forced RLS. References between tenant rows use composite foreign
  keys ``(tenant_id, x_id) -> x(tenant_id, id)`` (FK checks bypass RLS).

Migrations: 20261001_31 (candidates core) .. 20261001_35 (tags, lists).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

CANDIDATE_SOURCES = (
    "manual",
    "resume_upload",
    "bulk_import",
    "career_portal",
    "referral",
    "job_board",
    "other",
    # Epic 36 (300_01): Phase 3 sources (campus/event registrations: Epic 43; internal mobility: Epic 41).
    "campus",
    "event",
    "internal",
    # Epic 66 (500_01): Phase 5 sources (Job Network applies: Epic 75; Public API creates: Epic 67).
    "job_network",
    "public_api",
)
CTC_UNITS = ("annual", "monthly", "daily", "hourly")
LINK_KINDS = ("linkedin", "github", "portfolio", "other")
SKILL_SOURCES = ("parsed", "manual")
# Epic 36 (300_03, AD-41): written by ``skills_service.recompute`` (Epic 37); a trigger fills NULL on insert.
SKILL_VERIFICATIONS = ("declared", "inferred", "verified")
TENANT_SKILL_STATUSES = ("pending", "approved", "rejected")
TEXT_STATUSES = ("ok", "image_only", "encrypted", "failed")
PARSE_STATUSES = ("parsed", "failed_manual", "unavailable", "skipped_disabled", "skipped_consent")
DUPLICATE_STATUSES = ("open", "merged", "kept_separate")
IMPORT_BATCH_STATUSES = ("queued", "processing", "completed")
IMPORT_ITEM_STATUSES = ("queued", "processing", "created", "linked", "needs_review", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fk(columns: list[str], target: str, name: str, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    """Composite tenant-safe FK ``(tenant_id, col) -> target(tenant_id, id)``."""
    return ForeignKeyConstraint(
        ["tenant_id", *columns], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _candidate_fk(table: str, column: str = "candidate_id") -> ForeignKeyConstraint:
    return _fk([column], "candidates", f"fk_{table}_{column}_candidates")


class _CandidateChild(AuditedMixin):
    """Child rows are audited under their candidate (entity id + crypto-shredding subject)."""

    __audit_entity_id__ = "candidate_id"
    __pii_subject__ = "candidate"


# ================================================================ platform skill catalog
class Skill(UUIDPk, Timestamps, Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("slug"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())


class SkillAlias(UUIDPk, Timestamps, Base):
    __tablename__ = "skill_aliases"
    __table_args__ = (UniqueConstraint("alias_norm"),)

    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias_norm: Mapped[str] = mapped_column(String(200), nullable=False)


# ================================================================ tenant skill catalog
class TenantSkill(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "tenant_skills"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", TENANT_SKILL_STATUSES), name="status"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parent_skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_from: Mapped[str] = mapped_column(String(30), nullable=False)


class TenantSkillAlias(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "tenant_skill_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "alias_norm"),
        _fk(["tenant_skill_id"], "tenant_skills", "fk_tenant_skill_aliases_tenant_skill_id_tenant_skills"),
        CheckConstraint("(skill_id IS NULL) <> (tenant_skill_id IS NULL)", name="exactly_one_skill"),
    )

    alias_norm: Mapped[str] = mapped_column(String(200), nullable=False)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True
    )
    tenant_skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ candidates
class Candidate(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidates_owner_id_users",
            ondelete="RESTRICT",
        ),
        _fk(["merged_into_id"], "candidates", "fk_candidates_merged_into_id_candidates", "RESTRICT"),
        CheckConstraint(_in("source", CANDIDATE_SOURCES), name="source"),
        CheckConstraint(f"current_ctc_unit IS NULL OR {_in('current_ctc_unit', CTC_UNITS)}", name="cur_unit"),
        CheckConstraint(
            f"expected_ctc_unit IS NULL OR {_in('expected_ctc_unit', CTC_UNITS)}", name="exp_unit"
        ),
        Index("ix_candidates_owner_id", "owner_id"),
        Index(
            "ix_candidates_full_name_trgm",
            "full_name",
            postgresql_using="gin",
            postgresql_ops={"full_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_candidates_current_employer_trgm",
            "current_employer",
            postgresql_using="gin",
            postgresql_ops={"current_employer": "gin_trgm_ops"},
        ),
    )
    # Personal values are encrypted in the audit log under the candidate's key (crypto-shredded on erasure).
    __pii_fields__ = (
        "full_name",
        "location",
        "current_title",
        "current_employer",
        "current_ctc_amount",
        "expected_ctc_amount",
        "availability_date",
        "last_working_date",
        "work_authorization",
        "preferred_locations",
        "source_detail",
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    current_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    current_employer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    total_experience_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_ctc_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    current_ctc_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    current_ctc_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    expected_ctc_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_ctc_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    expected_ctc_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    availability_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_working_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    work_authorization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    willing_to_relocate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    preferred_locations: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Epic 12 (documented cross-domain columns, written ONLY by ``app.services.privacy``):
    # ``ai_consent_state`` granted|pending|withdrawn|not_required (migration 20261001_121), and
    # ``erased_at`` set by the erasure run (profile anonymised, id/human_id kept).
    ai_consent_state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'"), index=True
    )
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateEmail(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_emails"
    __table_args__ = (
        _candidate_fk("candidate_emails"),
        Index("ix_candidate_emails_candidate_id", "candidate_id"),
        Index("ix_candidate_emails_tenant_id_email", "tenant_id", "email"),
    )
    __pii_fields__ = ("email",)

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class CandidatePhone(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_phones"
    __table_args__ = (
        _candidate_fk("candidate_phones"),
        Index("ix_candidate_phones_candidate_id", "candidate_id"),
        Index("ix_candidate_phones_tenant_id_phone_e164", "tenant_id", "phone_e164"),
    )
    __pii_fields__ = ("phone_e164",)

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class CandidateLink(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_links"
    __table_args__ = (
        _candidate_fk("candidate_links"),
        CheckConstraint(_in("kind", LINK_KINDS), name="kind"),
        Index("ix_candidate_links_candidate_id", "candidate_id"),
        Index("ix_candidate_links_tenant_id_url_norm", "tenant_id", "url_norm"),
    )
    __pii_fields__ = ("url_norm",)

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    url_norm: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)


class CandidateEmployment(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_employments"
    __pii_fields__ = ("employer", "title", "location", "description")  # Epic 12: encrypted in the audit log
    __table_args__ = (
        _candidate_fk("candidate_employments"),
        Index("ix_candidate_employments_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    employer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class CandidateEducation(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_educations"
    __pii_fields__ = ("institution", "degree", "field", "grade")  # Epic 12: encrypted in the audit log
    __table_args__ = (
        _candidate_fk("candidate_educations"),
        Index("ix_candidate_educations_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    institution: Mapped[str | None] = mapped_column(String(300), nullable=True)
    degree: Mapped[str | None] = mapped_column(String(200), nullable=True)
    field: Mapped[str | None] = mapped_column(String(200), nullable=True)
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(50), nullable=True)


class CandidateCertification(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_certifications"
    __pii_fields__ = ("name", "issuer", "credential_id")  # Epic 12: encrypted in the audit log
    __table_args__ = (
        _candidate_fk("candidate_certifications"),
        Index("ix_candidate_certifications_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    issued_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String(200), nullable=True)


class CandidateSkill(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_skills"
    __table_args__ = (
        _candidate_fk("candidate_skills"),
        _fk(["tenant_skill_id"], "tenant_skills", "fk_candidate_skills_tenant_skill_id_tenant_skills"),
        CheckConstraint("(skill_id IS NULL) <> (tenant_skill_id IS NULL)", name="exactly_one_skill"),
        CheckConstraint(_in("source", SKILL_SOURCES), name="source"),
        UniqueConstraint("candidate_id", "skill_id"),
        UniqueConstraint("candidate_id", "tenant_skill_id"),
        Index("ix_candidate_skills_skill_id", "skill_id"),
        Index("ix_candidate_skills_tenant_skill_id", "tenant_skill_id"),
        # Epic 36 (300_03, AD-41)
        CheckConstraint("proficiency IS NULL OR proficiency BETWEEN 1 AND 5", name="proficiency"),
        CheckConstraint(_in("verification", SKILL_VERIFICATIONS), name="verification"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="RESTRICT"), nullable=True
    )
    tenant_skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    last_used: Mapped[date | None] = mapped_column(Date, nullable=True)
    # ---- Epic 36 (300_03, AD-41): written afterwards only by skills_service.recompute (Epic 37). The BEFORE
    # INSERT trigger ``tos_skill_verification_default`` fills NULL from ``source`` (parsed -> inferred, else declared).
    proficiency: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    verification: Mapped[str] = mapped_column(String(10), nullable=False, server_default=FetchedValue())


# ================================================================ resumes
class CandidateResume(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    """A resume version (``candidate_id`` NULL = draft uploaded for create-from-resume)."""

    __tablename__ = "candidate_resumes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("candidate_id", "version"),
        _candidate_fk("candidate_resumes"),
        _fk(["file_id"], "stored_files", "fk_candidate_resumes_file_id_stored_files", "RESTRICT"),
        CheckConstraint(_in("text_status", TEXT_STATUSES), name="text_status"),
        CheckConstraint(_in("parse_status", PARSE_STATUSES), name="parse_status"),
        Index("ix_candidate_resumes_candidate_id", "candidate_id"),
        Index("ix_candidate_resumes_text_tsv", "text_tsv", postgresql_using="gin"),
    )
    __audit_exclude__ = ("extracted_text", "text_tsv", "parsed_data")

    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple'::regconfig, coalesce(extracted_text, ''))", persisted=True),
        nullable=True,
    )
    text_status: Mapped[str] = mapped_column(String(20), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(20), nullable=False)
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    parsed_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ duplicates / merges
class CandidateDuplicate(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "candidate_duplicates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "candidate_a_id", "candidate_b_id"),
        _candidate_fk("candidate_duplicates", "candidate_a_id"),
        _candidate_fk("candidate_duplicates", "candidate_b_id"),
        CheckConstraint("candidate_a_id < candidate_b_id", name="ordered_pair"),
        CheckConstraint(_in("status", DUPLICATE_STATUSES), name="status"),
        Index("ix_candidate_duplicates_candidate_b_id", "candidate_b_id"),
    )

    candidate_a_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_b_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(5, 3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateMerge(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "candidate_merges"
    __table_args__ = (
        _candidate_fk("candidate_merges", "survivor_id"),
        _candidate_fk("candidate_merges", "merged_id"),
        _fk(
            ["duplicate_id"], "candidate_duplicates", "fk_candidate_merges_duplicate_id_candidate_duplicates"
        ),
        Index("ix_candidate_merges_survivor_id", "survivor_id"),
        Index("ix_candidate_merges_merged_id", "merged_id"),
    )
    __audit_exclude__ = ("snapshot",)

    survivor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    merged_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    duplicate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    field_choices: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    merged_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    merged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    undo_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ bulk import
class ImportBatch(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", IMPORT_BATCH_STATUSES), name="status"),
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImportItem(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "import_items"
    __table_args__ = (
        _fk(["batch_id"], "import_batches", "fk_import_items_batch_id_import_batches"),
        _fk(["file_id"], "stored_files", "fk_import_items_file_id_stored_files", "RESTRICT"),
        _candidate_fk("import_items"),
        _fk(["duplicate_id"], "candidate_duplicates", "fk_import_items_duplicate_id_candidate_duplicates"),
        CheckConstraint(_in("status", IMPORT_ITEM_STATUSES), name="status"),
        Index("ix_import_items_batch_id", "batch_id"),
    )
    __audit_exclude__ = ("filename",)

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    duplicate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# ================================================================ tags / lists
class Tag(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("tenant_id", "name"), UniqueConstraint("tenant_id", "id"))

    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)


class CandidateTag(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_tags"
    __table_args__ = (
        _candidate_fk("candidate_tags"),
        _fk(["tag_id"], "tags", "fk_candidate_tags_tag_id_tags"),
        UniqueConstraint("candidate_id", "tag_id"),
        Index("ix_candidate_tags_tag_id", "tag_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tag_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class CandidateList(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "candidate_lists"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_lists_owner_id_users",
            ondelete="RESTRICT",
        ),
        Index("ix_candidate_lists_owner_id", "owner_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateListMember(UUIDPk, Timestamps, TenantMixin, _CandidateChild, Base):
    __tablename__ = "candidate_list_members"
    __table_args__ = (
        _fk(["list_id"], "candidate_lists", "fk_candidate_list_members_list_id_candidate_lists"),
        _candidate_fk("candidate_list_members"),
        UniqueConstraint("list_id", "candidate_id"),
        Index("ix_candidate_list_members_candidate_id", "candidate_id"),
    )

    list_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    added_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class CandidateListShare(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "candidate_list_shares"
    __table_args__ = (
        _fk(["list_id"], "candidate_lists", "fk_candidate_list_shares_list_id_candidate_lists"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_list_shares_user_id_users",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "team_id"],
            ["teams.tenant_id", "teams.id"],
            name="fk_candidate_list_shares_team_id_teams",
            ondelete="CASCADE",
        ),
        CheckConstraint("(user_id IS NULL) <> (team_id IS NULL)", name="exactly_one_target"),
        UniqueConstraint("list_id", "user_id"),
        UniqueConstraint("list_id", "team_id"),
    )

    list_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
