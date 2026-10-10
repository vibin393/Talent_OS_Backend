# ruff: noqa: E501
"""Epic 63 (story 63.6, FR-315): the Collections Agent log (append-only, not audited).

Every agent message, Proposal and decision for the Invoice / Client collection timeline. ``summary`` never carries
amounts or Contact PII. Internal only: tenant RLS + internal-only org RLS.

Revision ID: 20261009_410_03_coll_log
Revises: 20261009_410_02_coll_rules
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261009_410_03_coll_log"
down_revision: str | tuple[str, ...] | None = "20261009_410_02_coll_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "collection_agent_log"
EVENTS = (
    "reminder_sent", "message_proposed", "proposal_approved", "proposal_edited_approved", "proposal_rejected",
    "proposal_expired", "proposal_stale", "message_sent", "held", "released", "takeover",
)  # fmt: skip


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column(
            "document_ids", postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False
        ),
        sa.Column("event", sa.String(30), nullable=False),
        sa.Column("rule_step_id", sa.Uuid(), nullable=True),
        sa.Column("tone", sa.String(12), nullable=True),
        sa.Column("email_message_id", sa.Uuid(), nullable=True),
        sa.Column("collection_activity_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("actor", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("summary", sa.String(300), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{T}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{T}_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{T}_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"], ["clients.tenant_id", "clients.id"], name="fk_collection_agent_log_client_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "event IN (" + ", ".join(repr(e) for e in EVENTS) + ")", name=op.f("ck_collection_agent_log_event")
        ),
    )  # fmt: skip
    op.create_index("ix_collection_agent_log_tenant_id", T, ["tenant_id"])
    op.create_index("ix_collection_agent_log_client_at", T, ["client_id", "at"])
    op.create_index("ix_collection_agent_log_document_ids", T, ["document_ids"], postgresql_using="gin")
    op.create_index("ix_collection_agent_log_proposal_id", T, ["proposal_id"])
    enable_rls(T)
    enable_org_rls(T)


def downgrade() -> None:
    disable_org_rls(T)
    disable_rls(T)
    op.drop_table(T)
