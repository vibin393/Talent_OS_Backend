# ruff: noqa: E501
"""Email templates (+ seed), template variants, sent-email log, recipients and address status (Epic 10,
story 10.1) (tenant-owned, RLS).

Revision ID: 20261001_101
Revises: 20261001_62
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_101"
down_revision: str | tuple[str, ...] | None = "20261001_62"
branch_labels: str | Sequence[str] | None = ("comms",)
# The FK targets (users, candidates, contacts, demands, applications) are all created by ancestors of
# 20261001_62, so no extra ``depends_on`` is needed (declaring ancestors confuses ``upgrade heads``).
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "email_templates",
    "email_template_variants",
    "email_messages",
    "email_recipients",
    "email_address_status",
)

TEMPLATE_KEYS = (
    "submission",
    "interview_invite",
    "interview_reminder",
    "rejection",
    "offer",
    "application_confirmation",
    "ai_screening_invite",
    "client_feedback_request",
    "custom",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def _seed_templates() -> None:
    """One system template per key (+ English variant) for every existing tenant (before RLS is enabled)."""
    from app.services.comms.defaults import DEFAULT_TEMPLATES

    bind = op.get_bind()
    tenant_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants"))]
    for tenant_id in tenant_ids:
        for key, spec in DEFAULT_TEMPLATES.items():
            template_id = bind.execute(
                sa.text(
                    "INSERT INTO email_templates (id, tenant_id, key, name, description, is_system) "
                    "VALUES (gen_random_uuid(), :tenant_id, :key, :name, :description, true) "
                    "ON CONFLICT (tenant_id, key, name) DO NOTHING RETURNING id"
                ),
                {"tenant_id": tenant_id, "key": key, "name": spec["name"], "description": spec["description"]},
            ).scalar()
            if template_id is None:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO email_template_variants (id, tenant_id, template_id, language, subject, body_html, version) "
                    "VALUES (gen_random_uuid(), :tenant_id, :template_id, 'en', :subject, :body_html, 1)"
                ),
                {
                    "tenant_id": tenant_id,
                    "template_id": template_id,
                    "subject": spec["subject"],
                    "body_html": spec["body_html"],
                },
            )


def upgrade() -> None:
    op.create_table(
        "email_templates",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(_in("key", TEMPLATE_KEYS), name=op.f("ck_email_templates_key")),
        _tenant_fk("email_templates"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_templates")),
        sa.UniqueConstraint("tenant_id", "key", "name", name=op.f("uq_email_templates_tenant_id_key_name")),
    )
    op.create_index(op.f("ix_email_templates_tenant_id"), "email_templates", ["tenant_id"], unique=False)

    op.create_table(
        "email_template_variants",
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=5), server_default=sa.text("'en'"), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["email_templates.id"],
            name=op.f("fk_email_template_variants_template_id_email_templates"),
            ondelete="CASCADE",
        ),
        _tenant_fk("email_template_variants"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_template_variants")),
        sa.UniqueConstraint(
            "template_id", "language", name=op.f("uq_email_template_variants_template_id_language")
        ),
    )
    op.create_index(
        op.f("ix_email_template_variants_tenant_id"), "email_template_variants", ["tenant_id"], unique=False
    )

    op.create_table(
        "email_messages",
        sa.Column("sender_user_id", sa.Uuid(), nullable=True),
        sa.Column("transport", sa.String(length=20), server_default=sa.text("'platform'"), nullable=False),
        sa.Column("fallback_reason", sa.String(length=40), nullable=True),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("reply_to", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("template_variant_id", sa.Uuid(), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=10), server_default=sa.text("'process'"), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "related", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("origin", sa.String(length=10), server_default=sa.text("'user'"), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(_in("transport", ("mailbox_google", "mailbox_microsoft", "platform")), name=op.f("ck_email_messages_transport")),
        sa.CheckConstraint(_in("status", ("queued", "sent", "failed")), name=op.f("ck_email_messages_status")),
        sa.CheckConstraint(_in("category", ("process", "marketing")), name=op.f("ck_email_messages_category")),
        sa.CheckConstraint(_in("origin", ("user", "workflow", "system")), name=op.f("ck_email_messages_origin")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sender_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_email_messages_sender_user_id_users",
            ondelete="RESTRICT",
        ),
        _tenant_fk("email_messages"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_messages")),
    )
    op.create_index(op.f("ix_email_messages_tenant_id"), "email_messages", ["tenant_id"], unique=False)
    op.create_index(
        "ix_email_messages_sender_created", "email_messages", ["tenant_id", "sender_user_id", "created_at"], unique=False
    )
    op.create_index("ix_email_messages_provider_message_id", "email_messages", ["provider_message_id"], unique=False)
    op.create_index("ix_email_messages_related", "email_messages", ["related"], unique=False, postgresql_using="gin")

    op.create_table(
        "email_recipients",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=4), server_default=sa.text("'to'"), nullable=False),
        sa.Column("address", postgresql.CITEXT(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bounce_type", sa.String(length=10), nullable=True),
        sa.Column("bounce_reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(_in("kind", ("to", "cc")), name=op.f("ck_email_recipients_kind")),
        sa.CheckConstraint(
            _in("status", ("pending", "sent", "bounced", "suppressed")), name=op.f("ck_email_recipients_status")
        ),
        sa.CheckConstraint(
            "bounce_type IS NULL OR " + _in("bounce_type", ("hard", "soft", "complaint")),
            name=op.f("ck_email_recipients_bounce_type"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["email_messages.id"],
            name=op.f("fk_email_recipients_message_id_email_messages"),
            ondelete="CASCADE",
        ),
        _tenant_fk("email_recipients"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_recipients")),
    )
    op.create_index(op.f("ix_email_recipients_tenant_id"), "email_recipients", ["tenant_id"], unique=False)
    op.create_index("ix_email_recipients_message_id", "email_recipients", ["message_id"], unique=False)
    op.create_index("ix_email_recipients_address", "email_recipients", ["tenant_id", "address"], unique=False)
    op.create_index(
        "ix_email_recipients_entity", "email_recipients", ["tenant_id", "entity_type", "entity_id"], unique=False
    )

    op.create_table(
        "email_address_status",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("address", postgresql.CITEXT(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("hard_bounced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_bounce_reason", sa.String(length=500), nullable=True),
        sa.Column("marketing_opt_out_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        _tenant_fk("email_address_status"),
        sa.PrimaryKeyConstraint("tenant_id", "address", name=op.f("pk_email_address_status")),
        sa.UniqueConstraint("id", name=op.f("uq_email_address_status_id")),
    )

    _seed_templates()
    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
