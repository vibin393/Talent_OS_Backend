# ruff: noqa: E501
"""Epic 35 222_03: ``portal_notification_preferences`` (FR-184).

External Principals' per-type email switch. The P1 ``notification_preferences`` is keyed by ``user_id`` (FK to
``users``), so a separate table is used instead of relaxing it (deviation recorded in the spec). Tenant RLS; the
portal inbox service filters by ``recipient_kind`` + ``recipient_id``.

Revision ID: 20261005_222_03_notif_portal
Revises: 20261005_222_02_golive
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261005_222_03_notif_portal"
down_revision: str | tuple[str, ...] | None = "20261005_222_02_golive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_notification_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("recipient_kind", sa.String(20), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("email", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portal_notification_preferences")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_portal_notification_preferences_tenant_id_tenants"), ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "recipient_kind", "recipient_id", "type", name="uq_portal_notif_prefs_recipient_type"),
        sa.CheckConstraint("recipient_kind IN ('candidate', 'client_user', 'vendor_user')", name=op.f("ck_portal_notification_preferences_recipient_kind")),
    )
    op.create_index(op.f("ix_portal_notification_preferences_tenant_id"), "portal_notification_preferences", ["tenant_id"])
    enable_rls("portal_notification_preferences")


def downgrade() -> None:
    disable_rls("portal_notification_preferences")
    op.drop_index(op.f("ix_portal_notification_preferences_tenant_id"), table_name="portal_notification_preferences")
    op.drop_table("portal_notification_preferences")
