# ruff: noqa: E501
"""Epic 75 (Cross-tenant Job Network, stories 75.1 / 75.4 / 75.5, spec A1): the public index.

Platform tables (no RLS; explicit ``talent_app`` grants; no Candidate data): ``network_members``, ``network_listings``,
``network_reports`` and the append-only ``network_moderation_actions`` (``GRANT SELECT, INSERT`` only). Tenant tables
``job_network_settings`` / ``job_network_choices`` (RLS + internal-only org RLS).

Revision ID: 20261020_509_01_job_network
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls
from app.core.settings import get_settings

revision: str = "20261020_509_01_job_network"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_job_network",)
depends_on: str | Sequence[str] | None = None

MEMBER_STATUSES = ("pending", "active", "suspended", "left")
VERIFICATION_STATUSES = ("unverified", "pending", "verified", "rejected")
LISTING_STATUSES = ("pending_checks", "listed", "held", "suspended", "unlisted", "removed")
REPORT_REASONS = ("scam_fee", "fake_job", "offensive", "discriminatory", "wrong_info", "duplicate", "other")
REPORT_STATUSES = ("open", "upheld", "dismissed")
MODERATION_TARGETS = ("listing", "member")
MODERATION_ACTIONS = (
    "verify", "reject_verification", "suspend", "reinstate", "remove", "uphold_report", "dismiss_report",
    "release_hold",
)  # fmt: skip
LIST_MODES = ("all", "choose")

PLATFORM_TABLES = ("network_members", "network_listings", "network_reports")
TENANT_TABLES = ("job_network_settings", "job_network_choices")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _stamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    role = get_settings().db_app_user

    # ---------------------------------------------------------------- network_members
    t = "network_members"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("public_slug", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("about_text", sa.String(2000), nullable=True),
        sa.Column("careers_base_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("verification_status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_reason", sa.String(500), nullable=True),
        sa.Column("violation_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("listing_limit", sa.Integer(), server_default=sa.text("500"), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", name=f"uq_{t}_tenant_id"),
        sa.UniqueConstraint("public_slug", name=f"uq_{t}_public_slug"),
        _ck(t, "status", _in("status", MEMBER_STATUSES)),
        _ck(t, "verification_status", _in("verification_status", VERIFICATION_STATUSES)),
    )

    # ---------------------------------------------------------------- network_listings
    t = "network_listings"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("posting_ref", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'pending_checks'"), nullable=False),
        sa.Column("hold_reasons", postgresql.ARRAY(sa.String(40)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description_html", sa.Text(), nullable=False),
        sa.Column("description_text", sa.Text(), nullable=False),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("region", sa.String(120), nullable=True),
        sa.Column("country", sa.CHAR(2), nullable=True),
        sa.Column("location_hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("work_mode", sa.String(10), nullable=True),
        sa.Column("employment_type", sa.String(20), nullable=True),
        sa.Column("experience_min", sa.Numeric(4, 1), nullable=True),
        sa.Column("experience_max", sa.Numeric(4, 1), nullable=True),
        sa.Column("skills", postgresql.ARRAY(sa.String(80)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("pay_min", sa.Numeric(14, 2), nullable=True),
        sa.Column("pay_max", sa.Numeric(14, 2), nullable=True),
        sa.Column("pay_currency", sa.CHAR(3), nullable=True),
        sa.Column("pay_period", sa.String(10), nullable=True),
        sa.Column("apply_url", sa.Text(), nullable=False),
        sa.Column("posting_url", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("listed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("search_tsv", postgresql.TSVECTOR(), server_default=sa.text("''::tsvector"), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["network_members.id"], name=f"fk_{t}_member_id_network_members", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("tenant_id", "posting_ref", name=f"uq_{t}_tenant_id_posting_ref"),
        _ck(t, "status", _in("status", LISTING_STATUSES)),
    )
    op.create_index("ix_network_listings_search_tsv", t, ["search_tsv"], postgresql_using="gin")
    op.create_index("ix_network_listings_status_published", t, ["status", sa.text("published_at DESC")])
    op.create_index("ix_network_listings_status_geo", t, ["status", "country", "region", "city"])
    op.create_index("ix_network_listings_skills", t, ["skills"], postgresql_using="gin")
    op.create_index("ix_network_listings_member_status", t, ["member_id", "status"])
    op.create_index(
        "ix_network_listings_listed_published", t, [sa.text("published_at DESC")],
        postgresql_where=sa.text("status = 'listed'"),
    )

    # ---------------------------------------------------------------- network_reports
    t = "network_reports"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("details", sa.String(1000), nullable=True),
        sa.Column("ip_hash", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["listing_id"], ["network_listings.id"], name=f"fk_{t}_listing_id_network_listings", ondelete="CASCADE"
        ),
        _ck(t, "reason", _in("reason", REPORT_REASONS)),
        _ck(t, "status", _in("status", REPORT_STATUSES)),
    )
    op.create_index("ix_network_reports_status_created", t, ["status", "created_at"])
    op.create_index("ix_network_reports_listing_id", t, ["listing_id"])

    # ---------------------------------------------------------------- network_moderation_actions (append-only)
    t = "network_moderation_actions"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(10), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _ck(t, "target_type", _in("target_type", MODERATION_TARGETS)),
        _ck(t, "action", _in("action", MODERATION_ACTIONS)),
    )
    op.create_index("ix_network_moderation_actions_target", t, ["target_type", "target_id"])

    for table in PLATFORM_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON network_moderation_actions TO {role}")

    # ---------------------------------------------------------------- job_network_settings (tenant)
    t = "job_network_settings"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("joined", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("list_mode", sa.String(10), server_default=sa.text("'choose'"), nullable=False),
        sa.Column("terms_version", sa.String(20), nullable=True),
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terms_accepted_by", sa.Uuid(), nullable=True),
        sa.Column("show_logo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("exclude_client_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["terms_accepted_by"], ["users.id"], name=f"fk_{t}_terms_accepted_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name=f"fk_{t}_updated_by_users", ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", name=f"uq_{t}_tenant_id"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _ck(t, "list_mode", _in("list_mode", LIST_MODES)),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- job_network_choices (tenant)
    t = "job_network_choices"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("demand_id", sa.Uuid(), nullable=False),
        sa.Column("posting_id", sa.Uuid(), nullable=False),
        sa.Column("include", sa.Boolean(), nullable=False),
        sa.Column("chosen_by", sa.Uuid(), nullable=True),
        sa.Column("chosen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "demand_id"], ["demands.tenant_id", "demands.id"],
            name=f"fk_{t}_demand_id_demands", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["chosen_by"], ["users.id"], name=f"fk_{t}_chosen_by_users", ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "demand_id", name=f"uq_{t}_tenant_id_demand_id"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
    )
    op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])
    op.create_index(f"ix_{t}_demand_id", t, ["demand_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in reversed(TENANT_TABLES):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
    op.drop_table("network_moderation_actions")
    op.drop_table("network_reports")
    op.drop_table("network_listings")
    op.drop_table("network_members")
