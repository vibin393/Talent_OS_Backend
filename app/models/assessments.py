# ruff: file-ignore[line-too-long]
"""Assessments (Epic 36 hub tables, AD-39; owner Epic 40, branch ``p3_assessments``).

* ``assessments``: tenant RLS only (the candidate portal reads the name through its own invitation).
* ``assessment_invitations``: worker org RLS on ``candidate_id`` (a candidate / worker session sees only its own
  invitations). ``status`` / ``result`` agree (CHECK); at most one open (invited / started) invitation per candidate
  and assessment (partial UNIQUE ``uq_assessment_invitations_open``).

Epic 40 adds the item bank (``assessment_items`` / immutable ``assessment_item_versions``), frozen
``assessment_versions``, attempts / answers / append-only proctoring signals / reviews, the provider catalogue cache
and skill map, Submission shares, per-tenant ``assessment_settings`` and item import batches; it ALTERs both hubs
(columns below, plus the ``assessment_version_id`` FK). Candidate code is never executed: coding items are scored by
comparison only (``services/assessments/scoring.py``). Attempts and their children are not audited (autosave) and
are reached by the Candidate Portal only through service queries filtered by the invitation's ``candidate_id``.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

ASSESSMENT_STATUSES = ("draft", "published", "archived")
ASSESSMENT_DELIVERIES = ("builtin", "provider")
INVITATION_CONTEXT_KINDS = ("application", "campus_drive", "event", "direct")
INVITATION_STATUSES = (
    "invited",
    "started",
    "expired",
    "completed",
    "reviewed",
    "passed",
    "failed",
    "cancelled",
)
INVITATION_OPEN_STATUSES = ("invited", "started")
INVITATION_RESULTS = ("pass", "fail")
# Epic 40
ITEM_KINDS = (
    "single", "multi", "true_false", "numeric", "short_text", "code_output", "code_fill", "code_order",
    "code_find_bug", "code_freeform", "essay",
)  # fmt: skip
REVIEW_ONLY_KINDS = frozenset({"code_freeform", "essay"})
ATTEMPT_STATUSES = ("in_progress", "submitted", "auto_submitted")
SIGNAL_KINDS = (
    "tab_switch",
    "focus_loss",
    "copy",
    "paste",
    "fullscreen_exit",
    "concurrent_session",
    "ip_change",
)
SCORED_VIA = ("auto", "review", "provider")
IMPORT_STATUSES = ("previewed", "committed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _text_array() -> Any:
    return mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))


class Assessment(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "assessments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("owner_id", "users", "fk_assessments_owner_id_users"),
        CheckConstraint(_in("status", ASSESSMENT_STATUSES), name="status"),
        CheckConstraint(_in("delivery", ASSESSMENT_DELIVERIES), name="delivery"),
        Index("ix_assessments_owner_id", "owner_id"),
        # Epic 40 (304_02)
        CheckConstraint("attempts_limit BETWEEN 1 AND 10", name="attempts_limit"),
        CheckConstraint("pass_mark_pct IS NULL OR pass_mark_pct BETWEEN 0 AND 100", name="pass_mark"),
        CheckConstraint(
            "auto_pass_threshold_pct IS NULL OR auto_pass_threshold_pct BETWEEN 0 AND 100", name="auto_pass"
        ),
        CheckConstraint(
            "time_limit_minutes IS NULL OR time_limit_minutes BETWEEN 1 AND 600", name="time_limit"
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    delivery: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'builtin'"))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 40 (304_02)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    provider_test_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pass_mark_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    shuffle_items: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    shuffle_options: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    attempts_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    gating_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    auto_pass_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    skill_keys: Mapped[list[str]] = _text_array()
    draft_sections: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class AssessmentInvitation(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "assessment_invitations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("assessment_id", "assessments", "fk_assessment_invitations_assessment_id_assessments"),
        _tenant_fk("candidate_id", "candidates", "fk_assessment_invitations_candidate_id_candidates"),
        _tenant_fk("application_id", "applications", "fk_assessment_invitations_application_id_applications"),
        CheckConstraint(_in("context_kind", INVITATION_CONTEXT_KINDS), name="context_kind"),
        CheckConstraint(_in("status", INVITATION_STATUSES), name="status"),
        CheckConstraint(f"result IS NULL OR {_in('result', INVITATION_RESULTS)}", name="result"),
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100", name="score"),
        CheckConstraint(
            "(status = 'passed' AND result IS NOT DISTINCT FROM 'pass') "
            "OR (status = 'failed' AND result IS NOT DISTINCT FROM 'fail') "
            "OR (status NOT IN ('passed', 'failed') AND result IS NULL)",
            name="status_result",
        ),
        CheckConstraint(
            "context_kind <> 'application' OR application_id IS NOT NULL", name="application_ctx"
        ),
        Index(
            "uq_assessment_invitations_open",
            "tenant_id",
            "assessment_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("status IN ('invited', 'started')"),
        ),
        Index("ix_assessment_invitations_assessment_id", "assessment_id"),
        Index("ix_assessment_invitations_candidate_id", "candidate_id"),
        Index("ix_assessment_invitations_application_id", "application_id"),
        # Epic 40 (304_03)
        _tenant_fk("assessment_version_id", "assessment_versions", "fk_assessment_invitations_version_id"),
        CheckConstraint("accommodation_factor BETWEEN 1.00 AND 3.00", name="accommodation"),
        Index(
            "uq_assessment_invitations_idem",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_assessment_invitations_status_deadline", "status", "deadline_at"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assessment_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Nullable since Epic 40 (304_03): erasure keeps an anonymous result row for percentile cohorts (A13).
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    context_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    context_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'invited'"))
    gating: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    result: Mapped[str | None] = mapped_column(String(10), nullable=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invited_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Epic 40 (304_03): accommodations, lifecycle, provider, results, decision.
    accommodation_factor: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text("1.00")
    )
    screen_reader_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extended_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    secure_link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    report_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    report_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    points: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    max_points: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    section_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    skill_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    percentile: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


# ================================================================ Epic 40: item bank (304_01)
class AssessmentItem(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "assessment_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id", name="uq_assessment_items_human_id"),
        CheckConstraint(_in("kind", ITEM_KINDS), name="kind"),
        CheckConstraint("difficulty BETWEEN 1 AND 5", name="difficulty"),
    )

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    skill_keys: Mapped[list[str]] = _text_array()
    tags: Mapped[list[str]] = _text_array()
    difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AssessmentItemVersion(UUIDPk, TenantMixin, AuditedMixin, Base):
    """Immutable (edits insert a new row). ``answer_key`` / ``rubric`` are server-only: never delivered to a
    candidate and never copied to the audit log."""

    __tablename__ = "assessment_item_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("item_id", "version", name="uq_assessment_item_versions_item_version"),
        _tenant_fk("item_id", "assessment_items", "fk_assessment_item_versions_item_id"),
        CheckConstraint("points > 0", name="points"),
        Index("ix_assessment_item_versions_item_id", "item_id"),
    )
    __audit_exclude__ = ("answer_key", "rubric")

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(40), nullable=True)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    points: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    answer_key: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rubric: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssessmentSettings(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (created lazily; defaults when absent)."""

    __tablename__ = "assessment_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_assessment_settings_tenant_id"),
        CheckConstraint("short_answer_confidence_threshold BETWEEN 0 AND 1", name="confidence"),
    )

    allow_auto_pass_non_gating: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    short_answer_confidence_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default=text("0.800")
    )
    reminder_hours_before: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("24"))
    default_deadline_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    link_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    max_signals_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60"))
    max_signals_per_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2000"))
    multi_choice_partial_credit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class AssessmentItemImport(UUIDPk, TenantMixin, Timestamps, Base):
    """CSV / XLSX item import batch: parsed rows with per-row errors (preview), then committed."""

    __tablename__ = "assessment_item_imports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(_in("status", IMPORT_STATUSES), name="status"),
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'previewed'"))
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rows: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ Epic 40: versions, provider cache (304_02)
class AssessmentVersion(UUIDPk, TenantMixin, AuditedMixin, Base):
    """Immutable published snapshot: fixed item version ids and, per draw section, the frozen candidate pool."""

    __tablename__ = "assessment_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("assessment_id", "version", name="uq_assessment_versions_assessment_version"),
        _tenant_fk("assessment_id", "assessments", "fk_assessment_versions_assessment_id"),
        Index("ix_assessment_versions_assessment_id", "assessment_id"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    total_points: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sections: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))


class AssessmentProviderTest(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "assessment_provider_tests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("config_id", "external_ref", name="uq_assessment_provider_tests_ref"),
        _tenant_fk(
            "config_id", "provider_connector_configs", "fk_assessment_provider_tests_config", "CASCADE"
        ),
    )

    config_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skills: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssessmentSkillMap(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "assessment_skill_map"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("config_id", "provider_skill", name="uq_assessment_skill_map_skill"),
        _tenant_fk("config_id", "provider_connector_configs", "fk_assessment_skill_map_config", "CASCADE"),
    )

    config_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_skill: Mapped[str] = mapped_column(String(200), nullable=False)
    skill_key: Mapped[str] = mapped_column(String(80), nullable=False)


# ================================================================ Epic 40: attempts (304_03)
class Attempt(UUIDPk, TenantMixin, Timestamps, Base):
    """One attempt of an invitation; at most one ``in_progress`` per invitation (AD-44)."""

    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("invitation_id", "attempt_no", name="uq_attempts_invitation_no"),
        _tenant_fk("invitation_id", "assessment_invitations", "fk_attempts_invitation_id"),
        CheckConstraint(_in("status", ATTEMPT_STATUSES), name="status"),
        Index(
            "uq_attempts_in_progress",
            "invitation_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
        Index("ix_attempts_status_deadline", "status", "deadline_at"),
    )

    invitation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'in_progress'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    section_plan: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    section_deadlines: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    current_section: Mapped[str | None] = mapped_column(String(40), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dropped_signals: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_score_total: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    max_points: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)


class AttemptAnswer(UUIDPk, TenantMixin, Base):
    __tablename__ = "attempt_answers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("attempt_id", "item_version_id", name="uq_attempt_answers_item"),
        UniqueConstraint("attempt_id", "position", name="uq_attempt_answers_position"),
        _tenant_fk("attempt_id", "attempts", "fk_attempt_answers_attempt_id", "CASCADE"),
        _tenant_fk("item_version_id", "assessment_item_versions", "fk_attempt_answers_item_version_id"),
        CheckConstraint(f"scored_via IS NULL OR {_in('scored_via', SCORED_VIA)}", name="scored_via"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    item_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    section_key: Mapped[str] = mapped_column(String(40), nullable=False)
    max_points: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    auto_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    auto_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    scored_via: Mapped[str | None] = mapped_column(String(10), nullable=True)


class ProctoringSignal(UUIDPk, TenantMixin, Base):
    """Append-only (the service has no update / delete path; only erasure deletes). Never read by scoring."""

    __tablename__ = "proctoring_signals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("attempt_id", "attempts", "fk_proctoring_signals_attempt_id", "CASCADE"),
        CheckConstraint(_in("kind", SIGNAL_KINDS), name="kind"),
        Index("ix_proctoring_signals_attempt_id", "attempt_id"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    client_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ProctoringAnnotation(UUIDPk, TenantMixin, Base):
    __tablename__ = "proctoring_annotations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("attempt_id", "attempts", "fk_proctoring_annotations_attempt_id", "CASCADE"),
        _tenant_fk("signal_id", "proctoring_signals", "fk_proctoring_annotations_signal_id", "CASCADE"),
        Index("ix_proctoring_annotations_attempt_id", "attempt_id"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReviewScore(UUIDPk, TenantMixin, Base):
    """Append-only; the latest row per answer sets ``attempt_answers.final_score``."""

    __tablename__ = "review_scores"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("answer_id", "attempt_answers", "fk_review_scores_answer_id", "CASCADE"),
        Index("ix_review_scores_answer_id", "answer_id"),
    )

    answer_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    rubric_marks: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubmissionAssessmentShare(UUIDPk, TenantMixin, Base):
    """FR-208: an invitation result the Recruiter chose to include in a Submission."""

    __tablename__ = "submission_assessment_shares"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("submission_id", "invitation_id", name="uq_submission_assessment_shares_pair"),
        _tenant_fk("submission_id", "submissions", "fk_submission_asm_shares_submission", "CASCADE"),
        _tenant_fk(
            "invitation_id", "assessment_invitations", "fk_submission_asm_shares_invitation", "CASCADE"
        ),
        Index("ix_submission_assessment_shares_invitation_id", "invitation_id"),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    invitation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "ASSESSMENT_DELIVERIES",
    "ASSESSMENT_STATUSES",
    "ATTEMPT_STATUSES",
    "INVITATION_CONTEXT_KINDS",
    "INVITATION_OPEN_STATUSES",
    "INVITATION_RESULTS",
    "INVITATION_STATUSES",
    "ITEM_KINDS",
    "REVIEW_ONLY_KINDS",
    "SIGNAL_KINDS",
    "Assessment",
    "AssessmentInvitation",
    "AssessmentItem",
    "AssessmentItemImport",
    "AssessmentItemVersion",
    "AssessmentProviderTest",
    "AssessmentSettings",
    "AssessmentSkillMap",
    "AssessmentVersion",
    "Attempt",
    "AttemptAnswer",
    "ProctoringAnnotation",
    "ProctoringSignal",
    "ReviewScore",
    "SubmissionAssessmentShare",
]
