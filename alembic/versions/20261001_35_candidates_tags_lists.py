"""Tags and saved candidate lists (story 3.7) (tenant-owned, RLS).

Revision ID: 20261001_35
Revises: 20261001_34
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls

revision: str = "20261001_35"
down_revision: str | None = "20261001_34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tags_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_tags_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_tags_tenant_id_name")),
    )
    op.create_index(op.f("ix_tags_tenant_id"), "tags", ["tenant_id"], unique=False)
    enable_rls("tags")

    op.create_table(
        "candidate_tags",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_tags_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tag_id"],
            ["tags.tenant_id", "tags.id"],
            name="fk_candidate_tags_tag_id_tags",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_tags_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_tags")),
        sa.UniqueConstraint("candidate_id", "tag_id", name=op.f("uq_candidate_tags_candidate_id_tag_id")),
    )
    op.create_index("ix_candidate_tags_tag_id", "candidate_tags", ["tag_id"], unique=False)
    op.create_index(op.f("ix_candidate_tags_tenant_id"), "candidate_tags", ["tenant_id"], unique=False)
    enable_rls("candidate_tags")

    op.create_table(
        "candidate_lists",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_lists_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_lists_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_lists")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_candidate_lists_tenant_id_id")),
    )
    op.create_index("ix_candidate_lists_owner_id", "candidate_lists", ["owner_id"], unique=False)
    op.create_index(op.f("ix_candidate_lists_tenant_id"), "candidate_lists", ["tenant_id"], unique=False)
    enable_rls("candidate_lists")

    op.create_table(
        "candidate_list_members",
        sa.Column("list_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_list_members_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "list_id"],
            ["candidate_lists.tenant_id", "candidate_lists.id"],
            name="fk_candidate_list_members_list_id_candidate_lists",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_list_members_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_list_members")),
        sa.UniqueConstraint(
            "list_id", "candidate_id", name=op.f("uq_candidate_list_members_list_id_candidate_id")
        ),
    )
    op.create_index(
        "ix_candidate_list_members_candidate_id", "candidate_list_members", ["candidate_id"], unique=False
    )
    op.create_index(
        op.f("ix_candidate_list_members_tenant_id"), "candidate_list_members", ["tenant_id"], unique=False
    )
    enable_rls("candidate_list_members")

    op.create_table(
        "candidate_list_shares",
        sa.Column("list_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NULL) <> (team_id IS NULL)", name=op.f("ck_candidate_list_shares_exactly_one_target")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "list_id"],
            ["candidate_lists.tenant_id", "candidate_lists.id"],
            name="fk_candidate_list_shares_list_id_candidate_lists",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "team_id"],
            ["teams.tenant_id", "teams.id"],
            name="fk_candidate_list_shares_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_list_shares_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_candidate_list_shares_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_list_shares")),
        sa.UniqueConstraint("list_id", "team_id", name=op.f("uq_candidate_list_shares_list_id_team_id")),
        sa.UniqueConstraint("list_id", "user_id", name=op.f("uq_candidate_list_shares_list_id_user_id")),
    )
    op.create_index(
        op.f("ix_candidate_list_shares_tenant_id"), "candidate_list_shares", ["tenant_id"], unique=False
    )
    enable_rls("candidate_list_shares")


def downgrade() -> None:
    op.drop_table("candidate_list_shares")
    op.drop_table("candidate_list_members")
    op.drop_table("candidate_lists")
    op.drop_table("candidate_tags")
    op.drop_table("tags")
