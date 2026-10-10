"""Clients, contacts and activities (Epic 2, stories 2.1-2.3): tenant-owned tables with forced RLS and
composite tenant foreign keys; trigram index for near-duplicate client names.

Revision ID: 20260930_07
Revises: 20261001_35
Create Date: 2026-09-30

(The revision id follows the batch plan; it chains after the Epic 3 revisions, which were the head when
this migration was written, so the history stays linear.)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20260930_07"
down_revision: str | None = "20261001_35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("industry", sa.String(length=200), nullable=True),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("locations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'prospect'"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('prospect', 'active', 'inactive')", name=op.f("ck_clients_status")),
        _tenant_fk("clients"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_clients_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clients")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_clients_tenant_id_name")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_clients_tenant_id_id")),
    )
    op.create_index(op.f("ix_clients_tenant_id"), "clients", ["tenant_id"])
    op.create_index("ix_clients_owner_id", "clients", ["owner_id"])
    op.execute("CREATE INDEX ix_clients_name_trgm ON clients USING gin ((name::text) gin_trgm_ops)")
    enable_rls("clients")

    op.create_table(
        "contacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("type", sa.String(length=20), server_default=sa.text("'other'"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "type IN ('hiring_manager', 'hr', 'procurement', 'finance', 'other')",
            name=op.f("ck_contacts_type"),
        ),
        _tenant_fk("contacts"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_contacts_client_id_clients",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_contacts_tenant_id_id")),
    )
    op.create_index(op.f("ix_contacts_tenant_id"), "contacts", ["tenant_id"])
    op.create_index("ix_contacts_client_id", "contacts", ["client_id"])
    op.create_index(
        "uq_contacts_client_id_email",
        "contacts",
        ["client_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    enable_rls("contacts")

    op.create_table(
        "activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("is_private", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("participants", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column(
            "mentions", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False
        ),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('note', 'call', 'meeting', 'email', 'system')", name=op.f("ck_activities_kind")
        ),
        _tenant_fk("activities"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "author_id"],
            ["users.tenant_id", "users.id"],
            name="fk_activities_author_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activities")),
    )
    op.create_index(op.f("ix_activities_tenant_id"), "activities", ["tenant_id"])
    op.create_index("ix_activities_entity", "activities", ["entity_type", "entity_id", "occurred_at", "id"])
    op.create_index("ix_activities_author_id", "activities", ["author_id"])
    enable_rls("activities")


def downgrade() -> None:
    op.drop_table("activities")
    op.drop_table("contacts")
    op.execute("DROP INDEX IF EXISTS ix_clients_name_trgm")
    op.drop_table("clients")
