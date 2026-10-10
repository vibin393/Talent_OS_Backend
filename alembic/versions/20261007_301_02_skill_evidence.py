# ruff: noqa: E501
"""Epic 37 story 37.2 (FR-188, AD-41): ``candidate_skill_evidence`` (+ baseline backfill), ``demand_skills``
``min_proficiency`` / ``expand`` (the only shared-table ALTER of Epic 37, build plan section 5) and the insight
index ``candidate_skills (skill_id, proficiency)``.

Backfill: one baseline evidence row per existing ``candidate_skills`` row -- ``self`` for ``source='manual'``,
``resume`` for ``parsed`` -- proficiency NULL, ``years`` / ``last_used`` copied, ``created_by_kind='system'``.

Revision ID: 20261007_301_02_skill_evidence
Revises: 20261007_301_01_skill_graph
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_301_02_skill_evidence"
down_revision: str | tuple[str, ...] | None = "20261007_301_01_skill_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "candidate_skill_evidence"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_skill_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("proficiency", sa.SmallInteger(), nullable=True),
        sa.Column("years", sa.Numeric(4, 1), nullable=True),
        sa.Column("last_used", sa.Date(), nullable=True),
        sa.Column("source_ref", sa.String(200), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_kind", sa.String(20), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_skill_evidence"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_candidate_skill_evidence_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_candidate_skill_evidence_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_skill_id"],
            ["candidate_skills.id"],
            name="fk_candidate_skill_evidence_candidate_skill_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_skill_evidence_candidate_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('resume', 'self', 'recruiter', 'assessment', 'project', 'graph')",
            name=op.f("ck_candidate_skill_evidence_kind"),
        ),
        sa.CheckConstraint(
            "proficiency IS NULL OR proficiency BETWEEN 1 AND 5", name=op.f("ck_candidate_skill_evidence_proficiency")
        ),
    )
    op.create_index("ix_candidate_skill_evidence_tenant_id", t, ["tenant_id"])
    op.create_index("ix_candidate_skill_evidence_candidate_skill_id", t, ["candidate_skill_id"])
    op.create_index("ix_candidate_skill_evidence_candidate_id", t, ["candidate_id"])
    op.create_index("ix_candidate_skill_evidence_source_ref", t, ["candidate_skill_id", "kind", "source_ref"])
    enable_rls(t)
    enable_org_rls(t)

    # Baseline evidence for every existing candidate skill (owner role, bypasses RLS).
    op.execute(
        "INSERT INTO candidate_skill_evidence (id, tenant_id, candidate_skill_id, candidate_id, kind, years, "
        "last_used, observed_at, created_by_kind) "
        "SELECT gen_random_uuid(), cs.tenant_id, cs.id, cs.candidate_id, "
        "CASE WHEN cs.source = 'parsed' THEN 'resume' ELSE 'self' END, cs.years, cs.last_used, cs.created_at, "
        "'system' FROM candidate_skills cs"
    )

    # demand_skills: minimum proficiency + per-skill expansion (P1 endpoints accept them additively).
    op.add_column("demand_skills", sa.Column("min_proficiency", sa.SmallInteger(), nullable=True))
    op.add_column(
        "demand_skills", sa.Column("expand", sa.Boolean(), server_default=sa.text("true"), nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_demand_skills_min_proficiency"),
        "demand_skills",
        "min_proficiency IS NULL OR min_proficiency BETWEEN 1 AND 5",
    )
    op.create_index("ix_candidate_skills_skill_id_proficiency", "candidate_skills", ["skill_id", "proficiency"])


def downgrade() -> None:
    op.drop_index("ix_candidate_skills_skill_id_proficiency", table_name="candidate_skills")
    op.drop_constraint(op.f("ck_demand_skills_min_proficiency"), "demand_skills", type_="check")
    op.drop_column("demand_skills", "expand")
    op.drop_column("demand_skills", "min_proficiency")
    disable_org_rls("candidate_skill_evidence")
    disable_rls("candidate_skill_evidence")
    op.drop_table("candidate_skill_evidence")
