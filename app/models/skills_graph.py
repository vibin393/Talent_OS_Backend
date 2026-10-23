# ruff: file-ignore[line-too-long]
"""Skills graph & proficiency tables (Epic 37, FR-187..FR-191, AD-41; branch ``p3_skills``).

* ``skill_edges`` -- platform edges between global ``skills`` (no RLS, SELECT-only for ``talent_app``, listed in
  ``core.tenancy.PLATFORM_TABLES``), seeded from ``services/skills/data/skill_graph_seed.yaml``.
* ``tenant_skill_edges`` -- tenant edges layered on top (RLS); each end is exactly one of a global skill or a
  tenant skill (the P1 ``candidate_skills`` pattern).
* ``skill_graph_settings`` -- one row per tenant (expansion defaults used by matching and search).
* ``candidate_skill_evidence`` -- the evidence log behind ``candidate_skills.proficiency`` / ``verification``.
* ``skill_suggestions`` -- inferred skills waiting for a Recruiter decision (AI, graph, project, assessment).

Migrations: ``20261007_301_01_skill_graph`` .. ``20261007_301_03_skill_suggest``.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

EDGE_KINDS = ("parent_of", "related_to", "implies")
EDGE_SOURCES = ("seed", "esco", "curated")
EVIDENCE_KINDS = ("resume", "self", "recruiter", "assessment", "project", "graph")
EVIDENCE_KINDS += ("interview", "interview_declared")  # Epic 60 (C-10, 407_03)
VERIFIED_KINDS = frozenset({"recruiter", "assessment", "interview"})  # Epic 60: + interview
DECLARED_KINDS = frozenset({"self", "interview_declared"})  # Epic 60: + interview_declared
INFERRED_KINDS = frozenset({"resume", "graph", "project"})
SUGGESTION_SOURCES = ("ai_resume", "graph", "project", "assessment", "interview")  # Epic 60: + interview
SUGGESTION_STATUSES = ("pending", "accepted", "rejected", "edited")
WEIGHT_CHECK = "weight > 0 AND weight <= 1"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


# ================================================================ platform graph
class SkillEdge(UUIDPk, Timestamps, Base):
    __tablename__ = "skill_edges"
    __table_args__ = (
        UniqueConstraint("from_skill_id", "to_skill_id", "kind"),
        CheckConstraint(_in("kind", EDGE_KINDS), name="kind"),
        CheckConstraint(_in("source", EDGE_SOURCES), name="source"),
        CheckConstraint(WEIGHT_CHECK, name="weight"),
        CheckConstraint("from_skill_id <> to_skill_id", name="not_self"),
        Index("ix_skill_edges_to_skill_id", "to_skill_id"),
    )

    from_skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    to_skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    seed_version: Mapped[str] = mapped_column(String(20), nullable=False)


# ================================================================ tenant graph
class TenantSkillEdge(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "tenant_skill_edges"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("from_tenant_skill_id", "tenant_skills", "fk_tenant_skill_edges_from_tenant_skill_id"),
        _tenant_fk("to_tenant_skill_id", "tenant_skills", "fk_tenant_skill_edges_to_tenant_skill_id"),
        CheckConstraint("(from_skill_id IS NULL) <> (from_tenant_skill_id IS NULL)", name="one_from"),
        CheckConstraint("(to_skill_id IS NULL) <> (to_tenant_skill_id IS NULL)", name="one_to"),
        CheckConstraint(_in("kind", EDGE_KINDS), name="kind"),
        CheckConstraint(WEIGHT_CHECK, name="weight"),
        CheckConstraint(
            "coalesce(from_skill_id, from_tenant_skill_id) <> coalesce(to_skill_id, to_tenant_skill_id)",
            name="not_self",
        ),
        Index(
            "uq_tenant_skill_edges_refs",
            "tenant_id",
            text("coalesce(from_skill_id, from_tenant_skill_id)"),
            text("coalesce(to_skill_id, to_tenant_skill_id)"),
            "kind",
            unique=True,
        ),
    )

    from_skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True
    )
    from_tenant_skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    to_skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True
    )
    to_tenant_skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class SkillGraphSettings(TenantMixin, Timestamps, AuditedMixin, Base):
    """One row per tenant (lazily created by ``services/skills/settings.py``)."""

    __tablename__ = "skill_graph_settings"
    __audit_entity_id__ = "tenant_id"
    __table_args__ = (
        CheckConstraint("match_depth BETWEEN 1 AND 2", name="match_depth"),
        CheckConstraint("min_weight > 0 AND min_weight <= 1", name="min_weight"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    expansion_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    match_depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    min_weight: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False, server_default=text("0.30"))
    inference_auto_suggest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


# ================================================================ evidence
class CandidateSkillEvidence(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "candidate_skill_evidence"
    __audit_entity_id__ = "candidate_id"
    __pii_subject__ = "candidate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["candidate_skill_id"],
            ["candidate_skills.id"],
            name="fk_candidate_skill_evidence_candidate_skill_id",
            ondelete="CASCADE",
        ),
        _tenant_fk("candidate_id", "candidates", "fk_candidate_skill_evidence_candidate_id"),
        CheckConstraint(_in("kind", EVIDENCE_KINDS), name="kind"),
        CheckConstraint("proficiency IS NULL OR proficiency BETWEEN 1 AND 5", name="proficiency"),
        Index("ix_candidate_skill_evidence_candidate_skill_id", "candidate_skill_id"),
        Index("ix_candidate_skill_evidence_candidate_id", "candidate_id"),
        Index("ix_candidate_skill_evidence_source_ref", "candidate_skill_id", "kind", "source_ref"),
    )

    candidate_skill_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    proficiency: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    last_used: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ================================================================ suggestions
class SkillSuggestion(UUIDPk, Timestamps, TenantMixin, AuditedMixin, Base):
    __tablename__ = "skill_suggestions"
    __audit_entity_id__ = "candidate_id"
    __pii_subject__ = "candidate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("candidate_id", "candidates", "fk_skill_suggestions_candidate_id"),
        _tenant_fk("tenant_skill_id", "tenant_skills", "fk_skill_suggestions_tenant_skill_id"),
        CheckConstraint("(skill_id IS NULL) <> (tenant_skill_id IS NULL)", name="exactly_one_skill"),
        CheckConstraint(_in("source", SUGGESTION_SOURCES), name="source"),
        CheckConstraint(_in("status", SUGGESTION_STATUSES), name="status"),
        CheckConstraint(
            "proposed_proficiency IS NULL OR proposed_proficiency BETWEEN 1 AND 5", name="proficiency"
        ),
        Index("ix_skill_suggestions_candidate_id", "candidate_id"),
        Index(
            "uq_skill_suggestions_pending",
            "tenant_id",
            "candidate_id",
            text("coalesce(skill_id, tenant_skill_id)"),
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True
    )
    tenant_skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proposed_proficiency: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(12), nullable=False)
    rationale: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resume_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
