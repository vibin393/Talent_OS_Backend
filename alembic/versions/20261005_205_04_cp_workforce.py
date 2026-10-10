# ruff: noqa: E501
"""Epic 18 205_04: compliance packs, their items and the client download log (FR-103; org RLS on ``client_id``,
items inherit it from the pack).

Revision ID: 20261005_205_04_cp_workforce
Revises: 20261005_205_03_cp_approvals
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_205_04_cp_workforce"
down_revision: str | tuple[str, ...] | None = "20261005_205_03_cp_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

P, I, D = "client_compliance_packs", "client_compliance_pack_items", "client_portal_downloads"


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_fk(t: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT")


def _tfk(t: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=f"fk_{t}_{column}_{target}"[:63], ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        P,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        _tenant_fk(P),
        _tfk(P, "client_id", "clients"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{P}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{P}_tenant_id_id"),
    )
    op.create_index(f"ix_{P}_tenant_id", P, ["tenant_id"])
    op.create_index(f"ix_{P}_client_id", P, ["client_id", "period_month"])
    op.create_table(
        I,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("pack_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("label", sa.String(200), nullable=False),
        *_ts(),
        sa.CheckConstraint("file_id IS NOT NULL OR document_id IS NOT NULL", name=op.f(f"ck_{I}_source")),
        _tenant_fk(I),
        _tfk(I, "pack_id", P, "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{I}"),
    )
    op.create_index(f"ix_{I}_tenant_id", I, ["tenant_id"])
    op.create_index(f"ix_{I}_pack_id", I, ["pack_id"])
    op.create_table(
        D,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("client_user_id", sa.Uuid(), nullable=True),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_hash", sa.String(64), nullable=True),
        _tenant_fk(D),
        _tfk(D, "client_id", "clients"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{D}"),
    )
    op.create_index(f"ix_{D}_tenant_id", D, ["tenant_id"])
    op.create_index(f"ix_{D}_client_id", D, ["client_id", "at"])
    op.create_index(f"ix_{D}_item_id", D, ["item_id"])
    for t in (P, I, D):
        enable_rls(t)
    enable_org_rls(P, client_col="client_id")
    enable_org_rls(I, client_col="client_id", inherit_from=InheritRef(P, "pack_id", ("client_id",)))
    enable_org_rls(D, client_col="client_id")


def downgrade() -> None:
    for t in (D, I, P):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
