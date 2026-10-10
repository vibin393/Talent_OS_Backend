# ruff: noqa: E501
"""Phase 2 Epic 33 (FR-172, FR-173): SMS / WhatsApp channel templates, messages and opt-outs.

* ``channel_templates`` (internal-only org RLS), ``channel_messages`` and ``channel_opt_outs`` (org RLS with
  ``worker_col=candidate_id`` so a candidate-portal session sees only its own rows).
* ``consent_notices``: the ``whatsapp`` notice is seeded per tenant (idempotent; Epic 35 seeds the rest).
* ``activities.kind`` CHECK widened with ``email_reply``, ``sms`` and ``whatsapp``.

Revision ID: 20261003_220_02_comms_channels
Revises: 20261003_220_01_comms_inbound
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_220_02_comms_channels"
down_revision: str | tuple[str, ...] | None = "20261003_220_01_comms_inbound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("channel_templates", "channel_messages", "channel_opt_outs")
OLD_KINDS = "kind IN ('note', 'call', 'meeting', 'email', 'system')"
NEW_KINDS = "kind IN ('note', 'call', 'meeting', 'email', 'system', 'email_reply', 'sms', 'whatsapp')"
WHATSAPP_NOTICE_TITLE = "WhatsApp messages"
WHATSAPP_NOTICE_BODY = (
    "We would like to send you transactional WhatsApp messages about your applications, interviews, joining, "
    "onboarding and timesheets (no marketing). You can stop them at any time by replying STOP, from the link in "
    "any message, or from the Privacy page of the candidate portal."
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "channel_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("variables", postgresql.ARRAY(sa.String(length=80)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("provider_template_ref", sa.String(length=200), nullable=True),
        sa.Column("language", sa.String(length=10), server_default=sa.text("'en'"), nullable=False),
        sa.Column("sender_id", sa.String(length=6), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("channel IN ('sms', 'whatsapp')", name=op.f("ck_channel_templates_channel")),
        sa.CheckConstraint(
            "purpose IN ('timesheet_reminder', 'onboarding_task', 'interview_reminder', 'joining_touchpoint', 'other_transactional')",
            name=op.f("ck_channel_templates_purpose"),
        ),
        sa.CheckConstraint("status IN ('draft', 'registered', 'archived')", name=op.f("ck_channel_templates_status")),
        _tenant_fk("channel_templates"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_templates")),
        sa.UniqueConstraint("tenant_id", "channel", "key", name=op.f("uq_channel_templates_tenant_id_channel_key")),
    )
    op.create_table(
        "channel_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("direction", sa.String(length=10), server_default=sa.text("'outbound'"), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("recipient_kind", sa.String(length=10), nullable=True),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("to_e164", sa.String(length=20), nullable=True),
        sa.Column("body_rendered", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("refusal_reason", sa.String(length=40), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("connector_job_id", sa.Uuid(), nullable=True),
        sa.Column("dispatch_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("related", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("origin", sa.String(length=10), server_default=sa.text("'user'"), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("sent_by", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deferred_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_reply_to_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("channel IN ('sms', 'whatsapp')", name=op.f("ck_channel_messages_channel")),
        sa.CheckConstraint("direction IN ('outbound', 'inbound')", name=op.f("ck_channel_messages_direction")),
        sa.CheckConstraint(
            "status IN ('queued', 'deferred_quiet_hours', 'sent', 'delivered', 'read', 'failed', 'refused', 'received', 'manual_pending')",
            name=op.f("ck_channel_messages_status"),
        ),
        sa.CheckConstraint("origin IN ('user', 'workflow', 'system')", name=op.f("ck_channel_messages_origin")),
        sa.CheckConstraint(
            "recipient_kind IS NULL OR recipient_kind IN ('candidate', 'contact', 'user')",
            name=op.f("ck_channel_messages_recipient_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["channel_templates.id"], name=op.f("fk_channel_messages_template_id_channel_templates"), ondelete="SET NULL"
        ),
        _tenant_fk("channel_messages"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_messages")),
    )
    op.create_index("ix_channel_messages_recipient", "channel_messages", ["tenant_id", "recipient_kind", "recipient_id", "created_at"])
    op.create_index("ix_channel_messages_phone", "channel_messages", ["tenant_id", "channel", "to_e164", "created_at"])
    op.create_index("ix_channel_messages_provider_message_id", "channel_messages", ["provider_message_id"])
    op.create_index("ix_channel_messages_status", "channel_messages", ["tenant_id", "status"])
    op.create_table(
        "channel_opt_outs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=True),
        sa.Column("phone_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("recipient_kind", sa.String(length=10), nullable=True),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("channel IN ('sms', 'whatsapp')", name=op.f("ck_channel_opt_outs_channel")),
        sa.CheckConstraint(
            "source IN ('stop_keyword', 'recipient_link', 'user', 'provider', 'candidate_portal')",
            name=op.f("ck_channel_opt_outs_source"),
        ),
        sa.CheckConstraint(
            "recipient_kind IS NULL OR recipient_kind IN ('candidate', 'contact', 'user')",
            name=op.f("ck_channel_opt_outs_recipient_kind"),
        ),
        _tenant_fk("channel_opt_outs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_opt_outs")),
    )
    op.create_index(
        "uq_channel_opt_outs_active",
        "channel_opt_outs",
        ["tenant_id", "channel", "phone_hash"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_channel_opt_outs_candidate_id", "channel_opt_outs", ["candidate_id"])
    for table in TABLES:
        op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])
        enable_rls(table)
    enable_org_rls("channel_templates")
    enable_org_rls("channel_messages", worker_col="candidate_id")
    enable_org_rls("channel_opt_outs", worker_col="candidate_id")

    op.drop_constraint(op.f("ck_activities_kind"), "activities", type_="check")
    op.create_check_constraint(op.f("ck_activities_kind"), "activities", NEW_KINDS)

    # Idempotent seed of the ``whatsapp`` consent notice for every existing tenant (owner role bypasses RLS).
    op.execute(
        sa.text(
            """
            INSERT INTO consent_notices (id, tenant_id, purpose, locale, version, title, body_md, active, created_at, updated_at)
            SELECT gen_random_uuid(), t.id, 'whatsapp', 'en', 1, :title, :body, true, now(), now()
            FROM tenants t
            WHERE NOT EXISTS (
                SELECT 1 FROM consent_notices n WHERE n.tenant_id = t.id AND n.purpose = 'whatsapp' AND n.locale = 'en'
            )
            """
        ).bindparams(title=WHATSAPP_NOTICE_TITLE, body=WHATSAPP_NOTICE_BODY)
    )


def downgrade() -> None:
    op.execute("DELETE FROM activities WHERE kind IN ('email_reply', 'sms', 'whatsapp')")
    op.drop_constraint(op.f("ck_activities_kind"), "activities", type_="check")
    op.create_check_constraint(op.f("ck_activities_kind"), "activities", OLD_KINDS)
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
    op.drop_table("channel_opt_outs")
    op.drop_table("channel_messages")
    op.drop_table("channel_templates")
