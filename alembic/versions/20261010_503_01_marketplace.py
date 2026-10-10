# ruff: noqa: E501
"""Epic 69 (Integration Marketplace, stories 69.1 / 69.5, spec A1): platform tables ``marketplace_listings``,
``marketplace_listing_versions``, ``marketplace_reviews`` (no tenant data; explicit ``talent_app`` grants) and the
additive ``platform_operators.roles`` column (CHECK admits ``marketplace_reviewer`` and ``network_moderator``).

Revision ID: 20261010_503_01_marketplace
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.settings import get_settings

revision: str = "20261010_503_01_marketplace"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_marketplace",)
depends_on: str | Sequence[str] | None = None

LISTING_KINDS = ("connector", "legacy_connector", "calendar", "app")
PUBLISHER_KINDS = ("platform", "partner", "tenant")
LISTING_STATUSES = ("draft", "published", "suspended", "delisted")
LISTING_CATEGORIES = (
    "job_boards", "hrms", "accounting", "payroll", "calendar", "telephony", "speech_to_text", "assessment", "bgv",
    "esignature", "einvoice", "messaging", "finance_compliance", "bi", "other",
)  # fmt: skip
VERSION_STATUSES = (
    "draft", "submitted", "automated_checks", "in_review", "changes_requested", "approved", "published", "rejected",
    "suspended", "delisted", "superseded",
)  # fmt: skip
REVIEW_ACTIONS = (
    "claim", "checklist", "approve", "request_changes", "reject", "suspend", "reinstate", "delist", "comment",
)  # fmt: skip
TABLES = ("marketplace_listings", "marketplace_listing_versions", "marketplace_reviews")


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

    t = "marketplace_listings"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("slug", postgresql.CITEXT(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("connector_key", sa.String(40), nullable=True),
        sa.Column("legacy_key", sa.String(40), nullable=True),
        sa.Column("app_id", sa.Uuid(), nullable=True),
        sa.Column("publisher_kind", sa.String(10), nullable=False),
        sa.Column("partner_org_id", sa.Uuid(), nullable=True),
        sa.Column("owner_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("tagline", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "supported_countries",
            postgresql.ARRAY(sa.CHAR(2)),
            server_default=sa.text("'{IN}'"),
            nullable=False,
        ),
        sa.Column("pricing_note", sa.String(300), nullable=True),
        sa.Column("support_email", postgresql.CITEXT(), nullable=True),
        sa.Column("support_url", sa.Text(), nullable=True),
        sa.Column("logo_file_key", sa.String(300), nullable=True),
        sa.Column("manage_path", sa.String(200), nullable=True),
        sa.Column("featured", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("install_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_reason", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name="fk_marketplace_listings_app_id_apps", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["partner_org_id"],
            ["partner_orgs.id"],
            name="fk_marketplace_listings_partner_org_id_partner_orgs",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("slug", name="uq_marketplace_listings_slug"),
        _ck(t, "kind", _in("kind", LISTING_KINDS)),
        _ck(t, "publisher_kind", _in("publisher_kind", PUBLISHER_KINDS)),
        _ck(t, "status", _in("status", LISTING_STATUSES)),
        _ck(t, "category", _in("category", LISTING_CATEGORIES)),
        _ck(t, "slug", "slug ~ '^[a-z0-9-]{2,60}$'"),
        _ck(
            t,
            "kind_ref",
            "((kind IN ('connector', 'calendar')) = (connector_key IS NOT NULL)) AND "
            "((kind = 'legacy_connector') = (legacy_key IS NOT NULL)) AND ((kind = 'app') = (app_id IS NOT NULL))",
        ),
        _ck(
            t,
            "publisher",
            "((publisher_kind = 'tenant') = (owner_tenant_id IS NOT NULL)) AND "
            "((publisher_kind = 'partner') = (partner_org_id IS NOT NULL)) AND "
            "(kind = 'app' OR publisher_kind = 'platform')",
        ),
    )
    op.create_index(
        "uq_marketplace_listings_connector",
        t,
        ["connector_key"],
        unique=True,
        postgresql_where=sa.text("connector_key IS NOT NULL"),
    )
    op.create_index(
        "uq_marketplace_listings_legacy",
        t,
        ["legacy_key"],
        unique=True,
        postgresql_where=sa.text("legacy_key IS NOT NULL"),
    )
    op.create_index(
        "uq_marketplace_listings_app",
        t,
        ["app_id"],
        unique=True,
        postgresql_where=sa.text("app_id IS NOT NULL"),
    )
    op.create_index("ix_marketplace_listings_status_category", t, ["status", "category"])

    t = "marketplace_listing_versions"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.CHAR(64), nullable=False),
        sa.Column("manifest_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("data_flows", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column(
            "egress_hosts", postgresql.ARRAY(sa.String(255)), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("description_md", sa.Text(), nullable=False),
        sa.Column(
            "screenshots", postgresql.ARRAY(sa.String(300)), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("privacy_url", sa.Text(), nullable=True),
        sa.Column("dpa_url", sa.Text(), nullable=True),
        sa.Column("india_transfer_statement", sa.Text(), nullable=True),
        sa.Column("security_questionnaire", postgresql.JSONB(), nullable=True),
        sa.Column("changelog", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("conformance_report", postgresql.JSONB(), nullable=True),
        sa.Column("check_results", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("submitted_by_kind", sa.String(10), nullable=True),
        sa.Column("submitted_by_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_message", sa.String(4000), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["marketplace_listings.id"],
            name="fk_marketplace_listing_versions_listing_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("listing_id", "version_no", name="uq_marketplace_listing_versions_no"),
        _ck(t, "status", _in("status", VERSION_STATUSES)),
        _ck(
            t,
            "submitted_by_kind",
            "submitted_by_kind IS NULL OR submitted_by_kind IN ('developer', 'platform')",
        ),
    )
    op.create_index("ix_marketplace_listing_versions_status_submitted", t, ["status", "submitted_at"])
    op.create_index("ix_marketplace_listing_versions_sha", t, ["listing_id", "manifest_sha256"])

    t = "marketplace_reviews"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("checklist", postgresql.JSONB(), nullable=True),
        sa.Column("message", sa.String(4000), nullable=True),
        sa.Column("internal_note", sa.String(4000), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["marketplace_listing_versions.id"],
            name="fk_marketplace_reviews_version_id",
            ondelete="CASCADE",
        ),
        _ck(t, "action", _in("action", REVIEW_ACTIONS)),
    )
    op.create_index("ix_marketplace_reviews_version_at", t, ["version_id", "at"])

    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")

    op.add_column(
        "platform_operators",
        sa.Column("roles", postgresql.ARRAY(sa.String(30)), server_default=sa.text("'{}'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_platform_operators_roles"),
        "platform_operators",
        "roles <@ ARRAY['marketplace_reviewer', 'network_moderator']::varchar[]",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_platform_operators_roles"), "platform_operators", type_="check")
    op.drop_column("platform_operators", "roles")
    for table in reversed(TABLES):
        op.drop_table(table)
