"""Epic 77 (Copilot in Portals, spec A1): ``portal_copilot_conversations``, ``portal_copilot_messages``,
``portal_copilot_feedback`` (org RLS by the owning organization: client / vendor / worker) and
``portal_copilot_settings`` (one row per tenant, internal-only org RLS). Message content, titles and feedback
comments are sealed (``vault_service.seal_field``); no FK to ``agent_runs`` (plain UUIDs, as P4).

Revision ID: 20261010_511_01_portal_copilot
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_511_01_portal_copilot"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_portal_copilot",)
depends_on: str | Sequence[str] | None = None

ORG_TABLES = ("portal_copilot_conversations", "portal_copilot_messages", "portal_copilot_feedback")
TABLES = (*ORG_TABLES, "portal_copilot_settings")
ONE_ORG = (
    "(principal_type = 'client_user') = (client_id IS NOT NULL) AND "
    "(principal_type = 'vendor_user') = (vendor_id IS NOT NULL) AND "
    "(principal_type = 'candidate') = (candidate_id IS NOT NULL)"
)
ONE_ORG_COLUMNS = (
    "((client_id IS NOT NULL)::int + (vendor_id IS NOT NULL)::int + (candidate_id IS NOT NULL)::int) = 1"
)


def _base(t: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _org_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
    ]


def _conv_fk(t: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "conversation_id"],
        ["portal_copilot_conversations.tenant_id", "portal_copilot_conversations.id"],
        name=f"fk_{t}_conversation",
        ondelete="CASCADE",
    )


def upgrade() -> None:
    t = "portal_copilot_conversations"
    op.create_table(
        t,
        *_base(t),
        sa.Column("principal_type", sa.String(12), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        *_org_columns(),
        sa.Column("agent_key", sa.String(30), nullable=False),
        sa.Column("title_sealed", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'open'"), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.CheckConstraint(
            "principal_type IN ('client_user', 'vendor_user', 'candidate')",
            name=op.f("ck_portal_copilot_conversations_principal_type"),
        ),
        sa.CheckConstraint(ONE_ORG, name=op.f("ck_portal_copilot_conversations_one_org")),
        sa.CheckConstraint(
            "status IN ('open', 'closed')", name=op.f("ck_portal_copilot_conversations_status")
        ),
        sa.CheckConstraint(
            "agent_key IN ('copilot_client', 'copilot_vendor', 'copilot_candidate')",
            name=op.f("ck_portal_copilot_conversations_agent_key"),
        ),
    )
    op.create_index(
        "ix_portal_copilot_conversations_principal",
        t,
        ["tenant_id", "principal_type", "principal_id", sa.text("last_message_at DESC")],
    )
    op.create_index("ix_portal_copilot_conversations_expires_at", t, ["expires_at"])

    t = "portal_copilot_messages"
    op.create_table(
        t,
        *_base(t),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        *_org_columns(),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content_sealed", sa.LargeBinary(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "conversation_id", "seq", name="uq_portal_copilot_messages_seq"),
        _conv_fk(t),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'event')", name=op.f("ck_portal_copilot_messages_role")
        ),
        sa.CheckConstraint(ONE_ORG_COLUMNS, name=op.f("ck_portal_copilot_messages_one_org")),
    )
    op.create_index("ix_portal_copilot_messages_run_id", t, ["run_id"])

    t = "portal_copilot_feedback"
    op.create_table(
        t,
        *_base(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        *_org_columns(),
        sa.Column("principal_type", sa.String(12), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.String(4), nullable=False),
        sa.Column("reason", sa.String(14), nullable=True),
        sa.Column("comment_sealed", sa.LargeBinary(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "principal_id", name="uq_portal_copilot_feedback_run"),
        _conv_fk(t),
        sa.CheckConstraint(
            "principal_type IN ('client_user', 'vendor_user', 'candidate')",
            name=op.f("ck_portal_copilot_feedback_principal_type"),
        ),
        sa.CheckConstraint(ONE_ORG, name=op.f("ck_portal_copilot_feedback_one_org")),
        sa.CheckConstraint("rating IN ('up', 'down')", name=op.f("ck_portal_copilot_feedback_rating")),
        sa.CheckConstraint(
            "reason IS NULL OR reason IN ('wrong', 'not_grounded', 'unhelpful', 'unsafe', 'other')",
            name=op.f("ck_portal_copilot_feedback_reason"),
        ),
    )

    t = "portal_copilot_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("vendor_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("candidate_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "questions_per_hour_external", sa.SmallInteger(), server_default=sa.text("30"), nullable=False
        ),
        sa.Column(
            "questions_per_hour_candidate", sa.SmallInteger(), server_default=sa.text("15"), nullable=False
        ),
        sa.Column("retention_days", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        sa.Column("handoff_queue_role_key", sa.String(40), nullable=True),
        sa.Column("disclosure_extra", sa.String(300), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", name="uq_portal_copilot_settings_tenant"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_portal_copilot_settings_updated_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "questions_per_hour_external BETWEEN 5 AND 120",
            name=op.f("ck_portal_copilot_settings_qph_external"),
        ),
        sa.CheckConstraint(
            "questions_per_hour_candidate BETWEEN 5 AND 60",
            name=op.f("ck_portal_copilot_settings_qph_candidate"),
        ),
        sa.CheckConstraint(
            "retention_days BETWEEN 7 AND 90", name=op.f("ck_portal_copilot_settings_retention")
        ),
    )
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
    for table in ORG_TABLES:
        enable_org_rls(table, client_col="client_id", vendor_col="vendor_id", worker_col="candidate_id")
    enable_org_rls("portal_copilot_settings")


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
