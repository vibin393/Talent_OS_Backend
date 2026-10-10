# ruff: noqa: E501
"""Epic 74 (Developer Portal, story 74.3, spec A2): platform tables ``developer_sandboxes`` (self-serve Sandbox Tenants
of a partner org or a Tenant) and ``developer_sandbox_members`` (the developer's user inside the sandbox).

Revision ID: 20261010_508_02_dev_sandboxes
Revises: 20261010_508_01_dev_accounts
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.settings import get_settings

revision: str = "20261010_508_02_dev_sandboxes"
down_revision: str | tuple[str, ...] | None = "20261010_508_01_dev_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("developer_sandboxes", "developer_sandbox_members")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    role = get_settings().db_app_user

    t = "developer_sandboxes"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("owner_kind", sa.String(12), nullable=False),
        sa.Column("partner_org_id", sa.Uuid(), nullable=True),
        sa.Column("owner_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_account_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("size", sa.String(6), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'creating'"), nullable=False),
        sa.Column("failure_code", sa.String(60), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["partner_org_id"], ["partner_orgs.id"], name="fk_developer_sandboxes_partner_org_id_partner_orgs",
            ondelete="CASCADE",
        ),
        _ck(t, "owner_kind", _in("owner_kind", ("partner_org", "tenant"))),
        _ck(t, "size", _in("size", ("small", "medium"))),
        _ck(t, "status", _in("status", ("creating", "ready", "resetting", "deleting", "deleted", "failed"))),
        _ck(
            t,
            "owner",
            "((owner_kind = 'partner_org') = (partner_org_id IS NOT NULL)) AND "
            "((owner_kind = 'tenant') = (owner_tenant_id IS NOT NULL))",
        ),
    )
    op.create_index("ix_developer_sandboxes_partner_org_id_status", t, ["partner_org_id", "status"])
    op.create_index("ix_developer_sandboxes_owner_tenant_id_status", t, ["owner_tenant_id", "status"])
    op.create_index("ix_developer_sandboxes_tenant_id", t, ["tenant_id"])

    t = "developer_sandbox_members"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("sandbox_user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["sandbox_id"], ["developer_sandboxes.id"], name="fk_developer_sandbox_members_sandbox_id",
            ondelete="CASCADE",
        ),
        # NULLs stay distinct (a tenant sandbox's members all have account_id NULL).
        sa.UniqueConstraint("sandbox_id", "account_id", name="uq_developer_sandbox_members_sandbox_account"),
        sa.UniqueConstraint("sandbox_id", "owner_user_id", name="uq_developer_sandbox_members_sandbox_owner_user"),
        _ck(t, "role", _in("role", ("admin", "viewer"))),
    )

    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
