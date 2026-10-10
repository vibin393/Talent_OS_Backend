# ruff: noqa: E501
"""Epic 37 story 37.3 (FR-189): ``skill_suggestions`` -- inferred skills (AI resume read, graph implication,
project, assessment) waiting for a Recruiter decision. One pending suggestion per (candidate, skill).

Revision ID: 20261007_301_03_skill_suggest
Revises: 20261007_301_02_skill_evidence
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_301_03_skill_suggest"
down_revision: str | tuple[str, ...] | None = "20261007_301_02_skill_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "skill_suggestions"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_skill_id", sa.Uuid(), nullable=True),
        sa.Column("proposed_proficiency", sa.SmallInteger(), nullable=True),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("rationale", sa.String(500), nullable=True),
        sa.Column("ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("resume_id", sa.Uuid(), nullable=True),
        sa.Column("source_ref", sa.String(200), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_skill_suggestions"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_skill_suggestions_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_skill_suggestions_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_skill_suggestions_candidate_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name="fk_skill_suggestions_skill_id_skills", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tenant_skill_id"],
            ["tenant_skills.tenant_id", "tenant_skills.id"],
            name="fk_skill_suggestions_tenant_skill_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(skill_id IS NULL) <> (tenant_skill_id IS NULL)", name=op.f("ck_skill_suggestions_exactly_one_skill")
        ),
        sa.CheckConstraint(
            "source IN ('ai_resume', 'graph', 'project', 'assessment')", name=op.f("ck_skill_suggestions_source")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'edited')", name=op.f("ck_skill_suggestions_status")
        ),
        sa.CheckConstraint(
            "proposed_proficiency IS NULL OR proposed_proficiency BETWEEN 1 AND 5",
            name=op.f("ck_skill_suggestions_proficiency"),
        ),
    )
    op.create_index("ix_skill_suggestions_tenant_id", t, ["tenant_id"])
    op.create_index("ix_skill_suggestions_candidate_id", t, ["candidate_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_skill_suggestions_pending ON skill_suggestions "
        "(tenant_id, candidate_id, coalesce(skill_id, tenant_skill_id)) WHERE status = 'pending'"
    )
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_org_rls("skill_suggestions")
    disable_rls("skill_suggestions")
    op.execute("DROP INDEX IF EXISTS uq_skill_suggestions_pending")
    op.drop_table("skill_suggestions")
