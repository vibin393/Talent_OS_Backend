"""Epic 5 search: ``f_unaccent``, the ``talent_en`` text-search configuration, the ``candidate_search_docs``
projection with its FTS / trigram / filter indexes, and the global-search trigram indexes on other domains'
tables (documented cross-module index exception, spec A6).

Branch ``epic5_search`` (parallel epic work; ``alembic upgrade heads``). ``down_revision`` = the single head
at the start of Epic 5 work (``20261001_44``).

Revision ID: 20261001_51
Revises: 20261001_44
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_51"
down_revision: str | tuple[str, ...] | None = "20261001_44"
branch_labels: str | Sequence[str] | None = ("epic5_search",)
depends_on: str | Sequence[str] | None = None

TABLE = "candidate_search_docs"

# Global search (A6): trigram / prefix indexes on other modules' tables, created only when missing.
# ``clients.name`` already has ``ix_clients_name_trgm`` (Epic 2), so it is not repeated here. These indexes
# are not declared on the owning models (cross-module exception), so ``alembic check`` lists them.
CROSS_MODULE_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_search_contacts_name_trgm", "CREATE INDEX IF NOT EXISTS {n} ON contacts USING gin (name gin_trgm_ops)"),
    (
        "ix_search_contacts_email_trgm",
        "CREATE INDEX IF NOT EXISTS {n} ON contacts USING gin ((email::text) gin_trgm_ops)",
    ),
    ("ix_search_demands_title_trgm", "CREATE INDEX IF NOT EXISTS {n} ON demands USING gin (title gin_trgm_ops)"),
    ("ix_search_demands_req_id_upper", "CREATE INDEX IF NOT EXISTS {n} ON demands (tenant_id, upper(req_id))"),
)


def upgrade() -> None:
    for ext in ("pg_trgm", "unaccent", "citext"):
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
        AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'talent_en') THEN
                CREATE TEXT SEARCH CONFIGURATION public.talent_en (COPY = pg_catalog.english);
                ALTER TEXT SEARCH CONFIGURATION public.talent_en
                    ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, english_stem;
            END IF;
        END
        $$
        """
    )

    op.create_table(
        TABLE,
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("name_norm", sa.Text(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("phone_e164", sa.String(length=20), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("current_employer", sa.String(length=200), nullable=True),
        sa.Column("current_title", sa.String(length=200), nullable=True),
        sa.Column("skill_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False),
        sa.Column("skill_names", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column(
            "certifications", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("tags", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("country", sa.String(length=200), nullable=True),
        sa.Column("state", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=200), nullable=True),
        sa.Column("work_auth", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("experience_years", sa.Numeric(4, 1), nullable=True),
        sa.Column("expected_comp_inr", sa.Numeric(14, 2), nullable=True),
        sa.Column("expected_comp_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("expected_comp_currency", sa.String(length=3), nullable=True),
        sa.Column("expected_comp_unit", sa.String(length=10), nullable=True),
        sa.Column("notice_days", sa.Integer(), nullable=True),
        sa.Column("available_from", sa.Date(), nullable=True),
        sa.Column(
            "submitted_client_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False
        ),
        sa.Column("ai_consent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("candidate_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resume_text", sa.Text(), nullable=True),
        sa.Column("doc", postgresql.TSVECTOR(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_search_docs_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_candidate_search_docs_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("candidate_id", name=op.f("pk_candidate_search_docs")),
    )
    op.create_index(op.f("ix_candidate_search_docs_tenant_id"), TABLE, ["tenant_id"])
    op.create_index(
        "ix_candidate_search_docs_doc", TABLE, ["doc"], postgresql_using="gin", postgresql_where=sa.text("NOT is_archived")
    )
    op.create_index(
        "ix_candidate_search_docs_name_norm_trgm",
        TABLE,
        ["name_norm"],
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_candidate_search_docs_email_trgm", TABLE, [sa.text("(email::text) gin_trgm_ops")], postgresql_using="gin"
    )
    op.create_index("ix_candidate_search_docs_tenant_id_phone_e164", TABLE, ["tenant_id", "phone_e164"])
    op.create_index("ix_candidate_search_docs_tenant_id_human_id", TABLE, ["tenant_id", "human_id"])
    for col in ("skill_ids", "tags", "work_auth", "submitted_client_ids"):
        op.create_index(f"ix_candidate_search_docs_{col}", TABLE, [col], postgresql_using="gin")
    op.create_index("ix_candidate_search_docs_tenant_id_country_city", TABLE, ["tenant_id", "country", "city"])
    for col in ("experience_years", "notice_days", "available_from", "expected_comp_inr", "owner_id"):
        op.create_index(f"ix_candidate_search_docs_tenant_id_{col}", TABLE, ["tenant_id", col])
    op.create_index(
        "ix_candidate_search_docs_tenant_id_candidate_updated_at",
        TABLE,
        ["tenant_id", sa.text("candidate_updated_at DESC")],
    )
    enable_rls(TABLE)

    for name, ddl in CROSS_MODULE_INDEXES:
        op.execute(ddl.format(n=name))


def downgrade() -> None:
    for name, _ in reversed(CROSS_MODULE_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    disable_rls(TABLE)
    op.drop_table(TABLE)
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS public.talent_en")
    op.execute("DROP FUNCTION IF EXISTS f_unaccent(text)")
