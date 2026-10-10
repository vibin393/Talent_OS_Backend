# ruff: noqa: E501
"""Phase 3 Epic 50 (Client Portal SSO, FR-250..FR-252, AD-48): client identity providers.

* ``identity_providers`` + ``audience`` (``internal`` | ``client``, default ``internal``), ``client_id`` (composite
  FK ``clients``; CHECK ``(audience = 'client') = (client_id IS NOT NULL)``), ``jit_default_permissions``.
  AD-48 ``allowed_domains`` is the existing ``email_domains`` column (no second domain column).
* ``client_user_identities`` (``(idp, subject)`` -> Client User), ``client_sso_settings`` (per-Client enforcement
  and approval-link opt-out), ``client_sso_exemptions`` (break-glass Client Users). Tenant RLS + the internal-only
  org policy (no Client User session ever reads them).

Revision ID: 20261007_314_01_client_idp
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_314_01_client_idp"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_client_sso",)
depends_on: str | Sequence[str] | None = None

TABLES = ("client_sso_exemptions", "client_sso_settings", "client_user_identities")


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- identity_providers ALTER
    t = "identity_providers"
    op.add_column(t, sa.Column("audience", sa.String(10), server_default=sa.text("'internal'"), nullable=False))
    op.add_column(t, sa.Column("client_id", sa.Uuid(), nullable=True))
    op.add_column(
        t,
        sa.Column(
            "jit_default_permissions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.create_check_constraint(op.f("ck_identity_providers_audience"), t, "audience IN ('internal', 'client')")
    op.create_check_constraint(
        op.f("ck_identity_providers_client_audience"), t, "(audience = 'client') = (client_id IS NOT NULL)"
    )
    op.create_foreign_key(
        "fk_identity_providers_client_id_clients",
        t,
        "clients",
        ["tenant_id", "client_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_identity_providers_client_id", t, ["client_id"])

    # ---------------------------------------------------------------- client_user_identities
    t = "client_user_identities"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_user_id", sa.Uuid(), nullable=False),
        sa.Column("idp_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email_at_link", sa.String(320), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        _fk(t, "idp_id", "identity_providers"),
        _fk(t, "client_user_id", "client_users", "CASCADE"),
        sa.UniqueConstraint("idp_id", "subject", name="uq_client_user_identities_idp_id_subject"),
        sa.UniqueConstraint("idp_id", "client_user_id", name="uq_client_user_identities_idp_id_client_user_id"),
    )
    op.create_index("ix_client_user_identities_tenant_id", t, ["tenant_id"])
    op.create_index("ix_client_user_identities_client_user_id", t, ["client_user_id"])
    enable_rls(t)
    enable_org_rls(t)  # no columns: internal sessions only

    # ---------------------------------------------------------------- client_sso_settings
    t = "client_sso_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("sso_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("approval_links_allowed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _fk(t, "client_id", "clients", "CASCADE"),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name="fk_client_sso_settings_updated_by_users", ondelete="SET NULL"
        ),
        sa.UniqueConstraint("tenant_id", "client_id", name="uq_client_sso_settings_tenant_id_client_id"),
    )
    op.create_index("ix_client_sso_settings_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    # ---------------------------------------------------------------- client_sso_exemptions
    t = "client_sso_exemptions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("client_user_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _fk(t, "client_user_id", "client_users", "CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_client_sso_exemptions_created_by_users", ondelete="SET NULL"
        ),
        sa.UniqueConstraint("client_user_id", name="uq_client_sso_exemptions_client_user_id"),
    )
    op.create_index("ix_client_sso_exemptions_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in TABLES:
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
    t = "identity_providers"
    op.drop_index("ix_identity_providers_client_id", table_name=t)
    op.drop_constraint("fk_identity_providers_client_id_clients", t, type_="foreignkey")
    op.drop_constraint(op.f("ck_identity_providers_client_audience"), t, type_="check")
    op.drop_constraint(op.f("ck_identity_providers_audience"), t, type_="check")
    op.drop_column(t, "jit_default_permissions")
    op.drop_column(t, "client_id")
    op.drop_column(t, "audience")
