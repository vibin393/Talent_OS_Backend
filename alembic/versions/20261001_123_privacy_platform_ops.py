"""Epic 12 platform operations: tenant lifecycle columns, support sessions, support access grants,
sub-processors (seeded), platform notices + acks, incidents + notifications, tenant exports and deletion
certificates.

Revision ID: 20261001_123
Revises: 20261001_122
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_123"
down_revision: str | tuple[str, ...] | None = "20261001_122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TENANT_COLUMNS = (
    ("status_reason", sa.Text()),
    ("suspended_at", sa.DateTime(timezone=True)),
    ("offboard_requested_at", sa.DateTime(timezone=True)),
    ("delete_after", sa.DateTime(timezone=True)),
    ("offboard_contact_email", postgresql.CITEXT()),
    ("deleted_at", sa.DateTime(timezone=True)),
)
SUB_PROCESSORS = (
    (
        "Cloud hosting provider (India region)",
        "Application hosting, database and file storage",
        ["all tenant data"],
        "India",
        "India",
        None,
    ),
    (
        "Anthropic PBC",
        "AI processing (resume parsing, JD generation, matching explanations, screening summaries) under "
        "contract; no training on customer data",
        ["candidate profiles", "resumes", "job descriptions", "screening answers"],
        "United States",
        "United States",
        "https://www.anthropic.com",
    ),
    (
        "Transactional email provider",
        "Delivery of system emails (invitations, notifications, secure links)",
        ["names", "email addresses", "email content"],
        "India",
        "India",
        None,
    ),
)


def upgrade() -> None:
    import uuid
    from datetime import UTC, datetime

    for name, type_ in TENANT_COLUMNS:
        op.add_column("tenants", sa.Column(name, type_, nullable=True))
    op.drop_constraint(op.f("ck_tenants_status"), "tenants", type_="check")
    op.create_check_constraint(
        "status", "tenants", "status IN ('active', 'suspended', 'offboarding', 'deleted')"
    )
    op.add_column("sessions", sa.Column("support_grant_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_sessions_support_grant_id"), "sessions", ["support_grant_id"], unique=False)
    op.drop_constraint(op.f("ck_sessions_principal_type"), "sessions", type_="check")
    op.create_check_constraint(
        "principal_type", "sessions", "principal_type IN ('user', 'candidate', 'platform', 'support')"
    )
    op.create_table(
        "support_access_grants",
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ticket_ref", sa.String(length=100), nullable=True),
        sa.Column("requested_hours", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'revoked', 'expired', 'cancelled')",
            name=op.f("ck_support_access_grants_status"),
        ),
        sa.CheckConstraint("char_length(reason) >= 20", name=op.f("ck_support_access_grants_reason_length")),
        sa.CheckConstraint("requested_hours BETWEEN 1 AND 24", name=op.f("ck_support_access_grants_hours")),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["platform_operators.id"],
            name=op.f("fk_support_access_grants_operator_id_platform_operators"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_support_access_grants_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_access_grants")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_support_access_grants_tenant_id_id")),
    )
    op.create_index(
        "ix_support_access_grants_operator_id", "support_access_grants", ["operator_id"], unique=False
    )
    op.create_index(
        op.f("ix_support_access_grants_tenant_id"), "support_access_grants", ["tenant_id"], unique=False
    )
    enable_rls("support_access_grants")
    op.create_table(
        "sub_processors",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column(
            "data_categories",
            sa.ARRAY(sa.String(length=100)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("processing_location", sa.String(length=200), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=False),
        sa.Column("website", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'planned'"), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notice_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emergency_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('planned', 'active', 'retired')", name=op.f("ck_sub_processors_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sub_processors")),
    )
    op.create_table(
        "platform_notices",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("audience", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.String(length=20), server_default=sa.text("'info'"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("audience IN ('admins', 'all_users')", name=op.f("ck_platform_notices_audience")),
        sa.CheckConstraint(
            "kind IN ('incident', 'sub_processor', 'maintenance')", name=op.f("ck_platform_notices_kind")
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'low', 'medium', 'high', 'critical')",
            name=op.f("ck_platform_notices_severity"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_platform_notices_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_notices")),
    )
    op.create_index("ix_platform_notices_tenant_id", "platform_notices", ["tenant_id"], unique=False)
    op.create_table(
        "platform_notice_acks",
        sa.Column("notice_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["notice_id"],
            ["platform_notices.id"],
            name=op.f("fk_platform_notice_acks_notice_id_platform_notices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("notice_id", "user_id", name=op.f("pk_platform_notice_acks")),
    )
    op.create_index(
        op.f("ix_platform_notice_acks_tenant_id"), "platform_notice_acks", ["tenant_id"], unique=False
    )
    op.create_table(
        "incidents",
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "severity IN ('info', 'low', 'medium', 'high', 'critical')", name=op.f("ck_incidents_severity")
        ),
        sa.CheckConstraint("status IN ('draft', 'notified', 'closed')", name=op.f("ck_incidents_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_table(
        "incident_notifications",
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("notice_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_by", sa.Uuid(), nullable=True),
        sa.Column("recipient_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("late", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_incident_notifications_incident_id_incidents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_incident_notifications_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident_notifications")),
        sa.UniqueConstraint(
            "incident_id", "tenant_id", name=op.f("uq_incident_notifications_incident_id_tenant_id")
        ),
    )
    op.create_index(
        op.f("ix_incident_notifications_tenant_id"), "incident_notifications", ["tenant_id"], unique=False
    )
    op.create_table(
        "tenant_exports",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("storage_key", sa.String(length=300), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('offboarding', 'admin')", name=op.f("ck_tenant_exports_kind")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed')", name=op.f("ck_tenant_exports_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_exports_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_exports")),
    )
    op.create_index("ix_tenant_exports_tenant_id", "tenant_exports", ["tenant_id"], unique=False)
    op.create_table(
        "tenant_deletion_certificates",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_name", sa.String(length=200), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("files_deleted", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_deletion_certificates")),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_tenant_deletion_certificates_tenant_id")),
    )
    op.create_table(
        "support_grant_lookups",
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("grant_id", name=op.f("pk_support_grant_lookups")),
    )
    op.create_index(op.f("ix_support_grant_lookups_tenant_id"), "support_grant_lookups", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_support_grant_lookups_operator_id"), "support_grant_lookups", ["operator_id"], unique=False)
    subs = sa.table(
        "sub_processors",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("purpose", sa.Text()),
        sa.column("data_categories", postgresql.ARRAY(sa.String())),
        sa.column("processing_location", sa.String()),
        sa.column("country", sa.String()),
        sa.column("website", sa.String()),
        sa.column("status", sa.String()),
        sa.column("effective_from", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        subs,
        [
            {
                "id": uuid.uuid4(),
                "name": name,
                "purpose": purpose,
                "data_categories": categories,
                "processing_location": location,
                "country": country,
                "website": website,
                "status": "active",
                "effective_from": datetime(2026, 10, 1, tzinfo=UTC),
            }
            for name, purpose, categories, location, country, website in SUB_PROCESSORS
        ],
    )


def downgrade() -> None:
    op.drop_table("support_grant_lookups")
    op.drop_table("tenant_deletion_certificates")
    op.drop_table("tenant_exports")
    op.drop_table("incident_notifications")
    op.drop_table("incidents")
    op.drop_table("platform_notice_acks")
    op.drop_table("platform_notices")
    op.drop_table("sub_processors")
    disable_rls("support_access_grants")
    op.drop_table("support_access_grants")
    op.execute("DELETE FROM sessions WHERE principal_type = 'support'")
    op.drop_constraint(op.f("ck_sessions_principal_type"), "sessions", type_="check")
    op.create_check_constraint(
        "principal_type", "sessions", "principal_type IN ('user', 'candidate', 'platform')"
    )
    op.drop_index(op.f("ix_sessions_support_grant_id"), table_name="sessions")
    op.drop_column("sessions", "support_grant_id")
    op.execute("UPDATE tenants SET status = 'suspended' WHERE status IN ('offboarding', 'deleted')")
    op.drop_constraint(op.f("ck_tenants_status"), "tenants", type_="check")
    op.create_check_constraint("status", "tenants", "status IN ('active', 'suspended')")
    for name, _ in reversed(TENANT_COLUMNS):
        op.drop_column("tenants", name)
