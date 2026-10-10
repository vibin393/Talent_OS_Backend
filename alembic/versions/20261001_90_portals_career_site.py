"""Epic 9 career site: ``career_sites`` (tenant-owned, RLS) and the Career Portal columns of
``demand_postings`` (slug, apply questions, search vector + GIN index, live-listing index), with a backfill of
slugs and search vectors for existing postings.

Revision ID: 20261001_90
Revises: 20261001_81
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_90"
down_revision: str | tuple[str, ...] | None = "20261001_81"
branch_labels: str | Sequence[str] | None = ("portals",)
# Epic 4 postings (20261001_43), Epic 6 pipeline (20261001_61) and privacy (20261001_121) are all ancestors of
# the down revision, so no extra ``depends_on`` is declared (declaring ancestors confuses ``upgrade heads``).
depends_on: str | Sequence[str] | None = None

BACKFILL_SQL = """
UPDATE demand_postings AS p SET
    slug = COALESCE(NULLIF(left(trim(both '-' from regexp_replace(lower(d.title), '[^a-z0-9]+', '-', 'g')), 80), ''), 'job'),
    search_tsv =
        setweight(to_tsvector('english', coalesce(d.title, '')), 'A')
        || setweight(to_tsvector('english', coalesce((
            SELECT string_agg(s.skill_label, ' ') FROM demand_skills s WHERE s.demand_id = d.id
        ), '')), 'B')
        || setweight(to_tsvector('english', coalesce(v.content, '')), 'C')
FROM demands AS d, jd_versions AS v
WHERE p.demand_id = d.id AND v.id = p.jd_version_id
"""


def upgrade() -> None:
    op.create_table(
        "career_sites",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("logo_file_id", sa.Uuid(), nullable=True),
        sa.Column("header_file_id", sa.Uuid(), nullable=True),
        sa.Column("brand_color", sa.String(length=7), nullable=True),
        sa.Column("about_md", sa.Text(), nullable=True),
        sa.Column("about_html", sa.Text(), nullable=True),
        sa.Column("privacy_contact_email", postgresql.CITEXT(), nullable=True),
        sa.Column(
            "candidate_stage_labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("apply_requires_phone", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "brand_color IS NULL OR brand_color ~ '^#[0-9A-Fa-f]{6}$'",
            name=op.f("ck_career_sites_brand_color"),
        ),
        sa.CheckConstraint(
            "about_md IS NULL OR char_length(about_md) <= 10000", name=op.f("ck_career_sites_about_len")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_career_sites_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_career_sites")),
    )
    enable_rls("career_sites")

    op.add_column("demand_postings", sa.Column("slug", sa.String(length=80), nullable=True))
    op.add_column(
        "demand_postings",
        sa.Column(
            "apply_questions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("demand_postings", sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=True))
    op.create_index(
        "ix_demand_postings_search_tsv",
        "demand_postings",
        ["search_tsv"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_demand_postings_tenant_live_published",
        "demand_postings",
        ["tenant_id", "is_live", sa.text("published_at DESC")],
        unique=False,
    )
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_index("ix_demand_postings_tenant_live_published", table_name="demand_postings")
    op.drop_index("ix_demand_postings_search_tsv", table_name="demand_postings")
    op.drop_column("demand_postings", "search_tsv")
    op.drop_column("demand_postings", "apply_questions")
    op.drop_column("demand_postings", "slug")
    disable_rls("career_sites")
    op.drop_table("career_sites")
