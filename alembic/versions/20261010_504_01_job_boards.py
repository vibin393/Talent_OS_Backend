# ruff: noqa: E501
"""Epic 70 (Job Board Connectors, stories 70.1-70.5, spec A1): ``board_postings``, ``board_sets``,
``board_field_mappings``, ``board_applicant_ingests``, ``board_ingest_suppressions``, ``board_credit_snapshots``,
``job_feed_settings`` and ``job_board_settings``. Tenant tables with RLS + org RLS (internal only).

D-18 (approved additive edit): ``apply_sessions`` gains ``src`` / ``src_ref`` (the keyed careers attribution registry
``portal.apply.register_apply_source_resolver``; Epic 75 reuses it).

Revision ID: 20261010_504_01_job_boards
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_504_01_job_boards"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_job_boards",)
depends_on: str | Sequence[str] | None = None

TABLES = (
    "board_postings", "board_sets", "board_field_mappings", "board_applicant_ingests", "board_ingest_suppressions",
    "board_credit_snapshots", "job_feed_settings", "job_board_settings",
)  # fmt: skip
BP_STATUSES = ("draft", "publishing", "live", "expiring", "expired", "closed", "failed", "abandoned", "rejected")
CLOSE_REASONS = ("user", "demand_closed", "demand_on_hold", "demand_filled", "posting_unpublished", "expired", "board")
MAPPING_KINDS = ("function", "industry", "location", "experience_band", "salary_band", "employment_type", "education", "default")
INGEST_STATUSES = ("received", "created", "linked", "suppressed", "failed")
SUPPRESSION_REASONS = ("erased", "consent_withdrawn", "do_not_contact")
RESUME_STATUSES = ("stored", "rejected", "missing")
EMPLOYMENT_TYPES = ("contract", "contract_to_hire", "permanent", "freelance")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _base(t: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def upgrade() -> None:
    t = "board_postings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("posting_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("config_id", sa.Uuid(), nullable=True),
        sa.Column("board_key", sa.String(40), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("local_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("jd_version_id", sa.Uuid(), nullable=False),
        sa.Column("board_fields", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("blocked_fields", postgresql.ARRAY(sa.String(60)), server_default=sa.text("'{}'::varchar(60)[]"), nullable=False),
        sa.Column("board_ref", sa.String(200), nullable=True),
        sa.Column("board_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("applies_reported", sa.Integer(), nullable=True),
        sa.Column("last_status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(60), nullable=True),
        sa.Column("last_error_message", sa.String(500), nullable=True),
        sa.Column("manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(20), nullable=True),
        sa.Column("cost_note", sa.String(200), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_board_postings_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_board_postings_human_id"),
        _fk("demand_id", "demands", "fk_board_postings_demand_id_demands", "CASCADE"),
        # demand_postings has no UNIQUE (tenant_id, id): a plain FK (RLS keeps tenants apart).
        sa.ForeignKeyConstraint(["posting_id"], ["demand_postings.id"], name="fk_board_postings_posting_id_demand_postings", ondelete="CASCADE"),
        _fk("installation_id", "app_installations", "fk_board_postings_installation_id_inst"),
        _fk("published_by", "users", "fk_board_postings_published_by_users"),
        sa.CheckConstraint(_in("status", BP_STATUSES), name=op.f("ck_board_postings_status")),
        sa.CheckConstraint("close_reason IS NULL OR " + _in("close_reason", CLOSE_REASONS), name=op.f("ck_board_postings_close_reason")),
        sa.CheckConstraint("revision <= local_revision", name=op.f("ck_board_postings_revision")),
    )
    op.create_index(
        "uq_board_postings_open", t, ["tenant_id", "posting_id", "board_key"], unique=True,
        postgresql_where=sa.text("status NOT IN ('closed', 'expired', 'abandoned')"),
    )  # fmt: skip
    op.create_index("ix_board_postings_demand_id", t, ["demand_id"])
    op.create_index("ix_board_postings_installation_status", t, ["installation_id", "status"])
    op.create_index("ix_board_postings_status_expires", t, ["status", "expires_at"])

    t = "board_sets"
    op.create_table(
        t,
        *_base(t),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("employment_type", sa.String(20), nullable=True),
        sa.Column("board_keys", postgresql.ARRAY(sa.String(40)), server_default=sa.text("'{}'::varchar(40)[]"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_board_sets_tenant_id_id"),
        sa.CheckConstraint("employment_type IS NULL OR " + _in("employment_type", EMPLOYMENT_TYPES), name=op.f("ck_board_sets_employment_type")),
    )
    op.create_index(
        "uq_board_sets_default", t, ["tenant_id", "employment_type"], unique=True,
        postgresql_where=sa.text("is_default"), postgresql_nulls_not_distinct=True,
    )  # fmt: skip

    t = "board_field_mappings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("board_key", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("local_value", sa.String(200), nullable=False),
        sa.Column("board_value", sa.String(200), nullable=False),
        sa.Column("board_label", sa.String(200), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_board_field_mappings_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "board_key", "kind", "local_value", name="uq_board_field_mappings_value"),
        sa.CheckConstraint(_in("kind", MAPPING_KINDS), name=op.f("ck_board_field_mappings_kind")),
    )

    t = "board_applicant_ingests"
    op.create_table(
        t,
        *_base(t),
        sa.Column("board_posting_id", sa.Uuid(), nullable=True),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("board_key", sa.String(40), nullable=False),
        sa.Column("board_application_id", sa.String(200), nullable=False),
        sa.Column("inbound_event_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'received'"), nullable=False),
        sa.Column("suppression_reason", sa.String(20), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("created_candidate", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("resume_status", sa.String(12), nullable=True),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_board_applicant_ingests_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "board_key", "board_application_id", name="uq_board_applicant_ingests_app"),
        _fk("board_posting_id", "board_postings", "fk_board_applicant_ingests_bp_id_board_postings", "SET NULL"),
        sa.CheckConstraint(_in("status", INGEST_STATUSES), name=op.f("ck_board_applicant_ingests_status")),
        sa.CheckConstraint("suppression_reason IS NULL OR " + _in("suppression_reason", SUPPRESSION_REASONS), name=op.f("ck_board_applicant_ingests_reason")),
        sa.CheckConstraint("resume_status IS NULL OR " + _in("resume_status", RESUME_STATUSES), name=op.f("ck_board_applicant_ingests_resume")),
    )
    op.create_index("ix_board_applicant_ingests_candidate_id", t, ["candidate_id"])
    op.create_index("ix_board_applicant_ingests_board_posting_id", t, ["board_posting_id"])

    t = "board_ingest_suppressions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("identifier_hash", sa.CHAR(64), nullable=False),
        sa.Column("kind", sa.String(6), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_board_ingest_suppressions_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "identifier_hash", name="uq_board_ingest_suppressions_hash"),
        sa.CheckConstraint(_in("kind", ("email", "phone")), name=op.f("ck_board_ingest_suppressions_kind")),
        sa.CheckConstraint(_in("reason", SUPPRESSION_REASONS), name=op.f("ck_board_ingest_suppressions_reason")),
    )

    t = "board_credit_snapshots"
    op.create_table(
        t,
        *_base(t),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("config_id", sa.Uuid(), nullable=True),
        sa.Column("credits_left", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_board_credit_snapshots_tenant_id_id"),
    )
    op.create_index("ix_board_credit_snapshots_inst_observed", t, ["installation_id", sa.text("observed_at DESC")])

    t = "job_feed_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("feed_token", sa.String(43), nullable=False),
        sa.Column("include_salary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("company_name_mode", sa.String(10), server_default=sa.text("'tenant'"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_job_feed_settings_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", name="uq_job_feed_settings_tenant_id"),
        sa.CheckConstraint(_in("company_name_mode", ("tenant", "client")), name=op.f("ck_job_feed_settings_company_name_mode")),
    )

    t = "job_board_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("expiry_warning_days", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("applicant_digest", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_close_on_demand_change", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_board_set_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_job_board_settings_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", name="uq_job_board_settings_tenant_id"),
        sa.CheckConstraint("expiry_warning_days BETWEEN 1 AND 14", name=op.f("ck_job_board_settings_expiry_days")),
    )

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)

    # D-18: careers attribution (``src`` / ``ref`` on the apply session).
    op.add_column("apply_sessions", sa.Column("src", sa.String(40), nullable=True))
    op.add_column("apply_sessions", sa.Column("src_ref", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("apply_sessions", "src_ref")
    op.drop_column("apply_sessions", "src")
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
