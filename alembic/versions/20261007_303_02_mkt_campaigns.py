# ruff: noqa: E501
"""Epic 39 stories 39.2-39.4 (FR-197, FR-198, FR-201): ``campaigns``, ``campaign_messages``,
``engagement_events``, ``campaign_attributions``. No P1/P2 ALTER: the foundation (300_01) already widened
``channel_templates.purpose`` (+marketing) and the ``email_messages`` / ``channel_messages`` origin CHECKs
(+campaign). ``campaign_messages.enrollment_id`` / ``step_id`` get their FKs in 303_03.

Revision ID: 20261007_303_02_mkt_campaigns
Revises: 20261007_303_01_mkt_audiences
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_303_02_mkt_campaigns"
down_revision: str | tuple[str, ...] | None = "20261007_303_01_mkt_audiences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("campaigns", "campaign_messages", "engagement_events", "campaign_attributions")
MESSAGE_STATUSES = (
    "('pending', 'deferred', 'queued', 'sent', 'delivered', 'bounced', 'failed', 'suppressed', 'cancelled')"
)
ENGAGEMENT_KINDS = "('delivered', 'bounced', 'complained', 'opened', 'clicked', 'replied', 'unsubscribed', 'applied', 'hired')"
CAMPAIGN_STATUSES = (
    "('draft', 'pending_approval', 'scheduled', 'sending', 'paused', 'completed', 'cancelled')"
)


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(name: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _finish(table: str) -> None:
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    enable_rls(table)
    enable_org_rls(table)


def upgrade() -> None:
    t = "campaigns"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("audience_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(500), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("email_subject", sa.String(500), nullable=True),
        sa.Column("email_body_html", sa.Text(), nullable=True),
        sa.Column("email_template_id", sa.Uuid(), nullable=True),
        sa.Column("channel_template_id", sa.Uuid(), nullable=True),
        sa.Column("job_links", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("ab_test", postgresql.JSONB(), nullable=True),
        sa.Column("counts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_campaigns"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaigns_tenant_id_id"),
        _fk("fk_campaigns_audience_id_audiences", "audience_id", "audiences"),
        _fk("fk_campaigns_snapshot_id", "snapshot_id", "audience_snapshots"),
        _fk("fk_campaigns_owner_id_users", "owner_id", "users"),
        sa.ForeignKeyConstraint(
            ["email_template_id"],
            ["email_templates.id"],
            name="fk_campaigns_email_template_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["channel_template_id"],
            ["channel_templates.id"],
            name="fk_campaigns_channel_template_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("channel IN ('email', 'sms', 'whatsapp')", name=op.f("ck_campaigns_channel")),
        sa.CheckConstraint(f"status IN {CAMPAIGN_STATUSES}", name=op.f("ck_campaigns_status")),
    )
    op.create_index("ix_campaigns_status", t, ["tenant_id", "status"])
    op.create_index("ix_campaigns_owner_id", t, ["owner_id"])
    _finish(t)

    t = "campaign_messages"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("enrollment_id", sa.Uuid(), nullable=True),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("variant_key", sa.String(4), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("suppression_reason", sa.String(40), nullable=True),
        sa.Column("defer_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_message_id", sa.Uuid(), nullable=True),
        sa.Column("channel_message_id", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_nonce", sa.String(32), nullable=False),
        sa.Column("pixel", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_messages"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaign_messages_tenant_id_id"),
        _fk("fk_campaign_messages_campaign_id", "campaign_id", "campaigns", "CASCADE"),
        _fk("fk_campaign_messages_candidate_id", "candidate_id", "candidates", "CASCADE"),
        sa.CheckConstraint(
            "channel IN ('email', 'sms', 'whatsapp')", name=op.f("ck_campaign_messages_channel")
        ),
        sa.CheckConstraint(f"status IN {MESSAGE_STATUSES}", name=op.f("ck_campaign_messages_status")),
        sa.CheckConstraint(
            "(campaign_id IS NOT NULL AND enrollment_id IS NULL AND step_id IS NULL)"
            " OR (campaign_id IS NULL AND enrollment_id IS NOT NULL AND step_id IS NOT NULL)",
            name=op.f("ck_campaign_messages_one_owner"),
        ),
    )
    op.create_index(
        "uq_campaign_messages_campaign_candidate",
        t,
        ["campaign_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("campaign_id IS NOT NULL"),
    )
    op.create_index(
        "uq_campaign_messages_enrollment_step",
        t,
        ["enrollment_id", "step_id"],
        unique=True,
        postgresql_where=sa.text("enrollment_id IS NOT NULL"),
    )
    op.create_index("ix_campaign_messages_candidate_sent", t, ["tenant_id", "candidate_id", "sent_at"])
    op.create_index("ix_campaign_messages_status_defer", t, ["status", "defer_until"])
    op.create_index("ix_campaign_messages_email_message_id", t, ["email_message_id"])
    op.create_index("ix_campaign_messages_channel_message_id", t, ["channel_message_id"])
    _finish(t)

    t = "engagement_events"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_tenant(t),
        sa.Column("campaign_message_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(14), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_engagement_events"),
        _fk("fk_engagement_events_message", "campaign_message_id", "campaign_messages", "CASCADE"),
        _fk("fk_engagement_events_candidate_id", "candidate_id", "candidates", "CASCADE"),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_engagement_events_dedupe_key"),
        sa.CheckConstraint(f"kind IN {ENGAGEMENT_KINDS}", name=op.f("ck_engagement_events_kind")),
    )
    op.create_index("ix_engagement_events_message_kind", t, ["campaign_message_id", "kind"])
    op.create_index("ix_engagement_events_candidate", t, ["tenant_id", "candidate_id", "kind", "occurred_at"])
    _finish(t)

    t = "campaign_attributions"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_message_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("sequence_id", sa.Uuid(), nullable=True),
        sa.Column("touched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attributed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("hired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("application_id", name="pk_campaign_attributions"),
        _fk("fk_campaign_attributions_application", "application_id", "applications", "CASCADE"),
        _fk("fk_campaign_attributions_message", "campaign_message_id", "campaign_messages", "CASCADE"),
        _fk("fk_campaign_attributions_candidate", "candidate_id", "candidates", "CASCADE"),
    )
    op.create_index("ix_campaign_attributions_campaign_id", t, ["campaign_id"])
    op.create_index("ix_campaign_attributions_sequence_id", t, ["sequence_id"])
    _finish(t)


def downgrade() -> None:
    for t in reversed(TABLES):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
