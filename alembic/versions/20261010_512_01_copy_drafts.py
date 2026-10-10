# ruff: noqa: E501
"""Epic 78 (Phase 4 hardening, stories 78.1-78.3, spec A1): ``campaign_copy_requests`` and ``campaign_copy_drafts``.
Tenant tables with RLS + org RLS (internal only). No column holds recipient data.

Revision ID: 20261010_512_01_copy_drafts
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261010_512_01_copy_drafts"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_p4_hardening",)
depends_on: str | Sequence[str] | None = None

TABLES = ("campaign_copy_requests", "campaign_copy_drafts")


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def _base(t: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="RESTRICT", name=f"fk_{t}_tenant_id_tenants"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    t = "campaign_copy_requests"
    op.create_table(
        t,
        *_base(t),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("sequence_id", sa.Uuid(), nullable=True),
        sa.Column("sequence_step_id", sa.Uuid(), nullable=True),
        sa.Column("step_position", sa.SmallInteger(), nullable=True),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("brief", postgresql.JSONB(), nullable=False),
        sa.Column("segments", postgresql.JSONB(), nullable=False),
        sa.Column("variants_per_segment", sa.SmallInteger(), nullable=False),
        sa.Column("channel_template_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("prompt_version", sa.String(20), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        _fk("campaign_id", "campaigns", "fk_campaign_copy_requests_campaign", "CASCADE"),
        _fk("sequence_id", "sequences", "fk_campaign_copy_requests_sequence", "CASCADE"),
        _fk("requested_by", "users", "fk_campaign_copy_requests_requested_by"),
        sa.CheckConstraint("(campaign_id IS NULL) <> (sequence_id IS NULL)", name=op.f("ck_campaign_copy_requests_one_target")),
        sa.CheckConstraint("sequence_id IS NULL OR step_position IS NOT NULL", name=op.f("ck_campaign_copy_requests_step_position")),
        sa.CheckConstraint("channel IN ('email', 'sms', 'whatsapp')", name=op.f("ck_campaign_copy_requests_channel")),
        sa.CheckConstraint(
            "status IN ('requested', 'generating', 'generated', 'failed', 'cancelled')", name=op.f("ck_campaign_copy_requests_status")
        ),
        sa.CheckConstraint("variants_per_segment BETWEEN 1 AND 3", name=op.f("ck_campaign_copy_requests_variants")),
    )
    op.create_index("ix_campaign_copy_requests_campaign", t, ["tenant_id", "campaign_id", "status"])
    op.create_index("ix_campaign_copy_requests_sequence", t, ["tenant_id", "sequence_id", "step_position"])
    op.create_index("ix_campaign_copy_requests_agent_run_id", t, ["agent_run_id"])

    t = "campaign_copy_drafts"
    op.create_table(
        t,
        *_base(t),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("sequence_id", sa.Uuid(), nullable=True),
        sa.Column("sequence_step_id", sa.Uuid(), nullable=True),
        sa.Column("step_position", sa.SmallInteger(), nullable=True),
        sa.Column("segment_key", sa.String(40), nullable=False),
        sa.Column("segment_label", sa.String(120), nullable=False),
        sa.Column("variant_no", sa.SmallInteger(), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("preheader", sa.String(200), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("template_values", postgresql.JSONB(), nullable=True),
        sa.Column("job_link_blocks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'generated'"), nullable=False),
        sa.Column("findings", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("warnings_acknowledged_by", sa.Uuid(), nullable=True),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("approved_sha256", sa.CHAR(64), nullable=True),
        sa.Column("ai_generated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("edited_by", sa.Uuid(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_ms", sa.Integer(), nullable=True),
        sa.Column("rubber_stamp", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("used_in_campaign_id", sa.Uuid(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "request_id", "segment_key", "variant_no", name="uq_campaign_copy_drafts_variant"),
        _fk("request_id", "campaign_copy_requests", "fk_campaign_copy_drafts_request", "CASCADE"),
        _fk("approved_by", "users", "fk_campaign_copy_drafts_approved_by"),
        _fk("edited_by", "users", "fk_campaign_copy_drafts_edited_by"),
        _fk("warnings_acknowledged_by", "users", "fk_campaign_copy_drafts_ack_by"),
        sa.CheckConstraint("channel IN ('email', 'sms', 'whatsapp')", name=op.f("ck_campaign_copy_drafts_channel")),
        sa.CheckConstraint(
            "status IN ('generated', 'checked', 'edited', 'approved', 'used', 'discarded')", name=op.f("ck_campaign_copy_drafts_status")
        ),
        sa.CheckConstraint("variant_no BETWEEN 1 AND 3", name=op.f("ck_campaign_copy_drafts_variant_no")),
        sa.CheckConstraint("body_html IS NULL OR octet_length(body_html) <= 51200", name=op.f("ck_campaign_copy_drafts_body_size")),
    )
    op.create_index("ix_campaign_copy_drafts_campaign", t, ["tenant_id", "campaign_id", "status"])
    op.create_index("ix_campaign_copy_drafts_sequence", t, ["tenant_id", "sequence_id", "step_position", "status"])
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
