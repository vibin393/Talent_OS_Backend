# ruff: noqa: E501
"""Epic 74 (Developer Portal, story 74.1, spec A2): ALTER ``partner_orgs`` (verification profile columns) and the
platform tables ``developer_accounts``, ``developer_recovery_codes``, ``developer_sessions``, ``developer_email_tokens``,
``partner_org_members``, ``partner_org_invitations``, ``partner_verification_requests`` and ``partner_org_audit_log``
(no RLS, no tenant data; explicit ``talent_app`` grants).

Revision ID: 20261010_508_01_dev_accounts
Revises: 20261009_500_06_p5_vault_drop
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.settings import get_settings

revision: str = "20261010_508_01_dev_accounts"
down_revision: str | tuple[str, ...] | None = "20261009_500_06_p5_vault_drop"
branch_labels: str | Sequence[str] | None = ("p5_dev_portal",)
depends_on: str | Sequence[str] | None = None

ORG_ROLES = ("owner", "developer", "viewer")
TABLES = (
    "developer_accounts",
    "developer_recovery_codes",
    "developer_sessions",
    "developer_email_tokens",
    "partner_org_members",
    "partner_org_invitations",
    "partner_verification_requests",
    "partner_org_audit_log",
)
ORG_COLUMNS = (
    "legal_name", "country", "tax_id_kind", "tax_id", "website", "support_email", "support_phone", "agreement_version",
    "agreement_accepted_at", "created_by_account_id",
)  # fmt: skip


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _id() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("id", sa.Uuid(), nullable=False)


def _stamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _fk(table: str, column: str, target: str, ondelete: str = "CASCADE") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], name=f"fk_{table}_{column}_{target}", ondelete=ondelete)


def upgrade() -> None:
    role = get_settings().db_app_user

    op.add_column("partner_orgs", sa.Column("legal_name", sa.String(200), nullable=True))
    op.add_column("partner_orgs", sa.Column("country", sa.CHAR(2), nullable=True))
    op.add_column("partner_orgs", sa.Column("tax_id_kind", sa.String(10), nullable=True))
    op.add_column("partner_orgs", sa.Column("tax_id", sa.String(40), nullable=True))
    op.add_column("partner_orgs", sa.Column("website", sa.Text(), nullable=True))
    op.add_column("partner_orgs", sa.Column("support_email", postgresql.CITEXT(), nullable=True))
    op.add_column("partner_orgs", sa.Column("support_phone", sa.String(20), nullable=True))
    op.add_column("partner_orgs", sa.Column("agreement_version", sa.String(20), nullable=True))
    op.add_column("partner_orgs", sa.Column("agreement_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("partner_orgs", sa.Column("created_by_account_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        op.f("ck_partner_orgs_tax_id_kind"),
        "partner_orgs",
        "tax_id_kind IS NULL OR tax_id_kind IN ('gstin', 'vat', 'ein', 'other')",
    )

    t = "developer_accounts"
    op.create_table(
        t,
        _id(),
        *_stamps(),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfa_secret_enc", sa.Text(), nullable=True),
        sa.Column("mfa_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfa_last_step", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("terms_version", sa.String(20), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("email", name=f"uq_{t}_email"),
        _ck(t, "status", _in("status", ("active", "locked", "closed"))),
    )

    t = "developer_recovery_codes"
    op.create_table(
        t,
        _id(),
        *_stamps(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.CHAR(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _fk(t, "account_id", "developer_accounts"),
        sa.UniqueConstraint("account_id", "code_hash", name=f"uq_{t}_account_id_code_hash"),
    )

    t = "developer_sessions"
    op.create_table(
        t,
        _id(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("mfa_passed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _fk(t, "account_id", "developer_accounts"),
        sa.UniqueConstraint("token_hash", name=f"uq_{t}_token_hash"),
    )
    op.create_index("ix_developer_sessions_account_id_revoked_at", t, ["account_id", "revoked_at"])

    t = "developer_email_tokens"
    op.create_table(
        t,
        _id(),
        *_stamps(),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _fk(t, "account_id", "developer_accounts"),
        sa.UniqueConstraint("token_hash", name=f"uq_{t}_token_hash"),
        _ck(t, "purpose", _in("purpose", ("verify_email", "reset_password"))),
    )

    t = "partner_org_members"
    op.create_table(
        t,
        _id(),
        *_stamps(),
        sa.Column("partner_org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _fk(t, "partner_org_id", "partner_orgs"),
        _fk(t, "account_id", "developer_accounts"),
        _ck(t, "role", _in("role", ORG_ROLES)),
    )
    op.create_index(
        "uq_partner_org_members_org_account_live", t, ["partner_org_id", "account_id"], unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )  # fmt: skip
    op.create_index("ix_partner_org_members_account_id", t, ["account_id"])

    t = "partner_org_invitations"
    op.create_table(
        t,
        _id(),
        *_stamps(),
        sa.Column("partner_org_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _fk(t, "partner_org_id", "partner_orgs"),
        sa.UniqueConstraint("token_hash", name=f"uq_{t}_token_hash"),
        _ck(t, "role", _in("role", ORG_ROLES)),
    )
    op.create_index("ix_partner_org_invitations_org_accepted", t, ["partner_org_id", "accepted_at"])

    t = "partner_verification_requests"
    op.create_table(
        t,
        _id(),
        *_stamps(),
        sa.Column("partner_org_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("evidence_file_keys", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("submitted_by", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.String(2000), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _fk(t, "partner_org_id", "partner_orgs"),
        _ck(t, "status", _in("status", ("draft", "pending", "changes_requested", "approved", "rejected"))),
    )
    op.create_index(
        "uq_partner_verification_requests_open", t, ["partner_org_id"], unique=True,
        postgresql_where=sa.text("status IN ('draft', 'pending', 'changes_requested')"),
    )  # fmt: skip

    t = "partner_org_audit_log"
    op.create_table(
        t,
        _id(),
        sa.Column("partner_org_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("operator_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(30), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
    )
    op.create_index("ix_partner_org_audit_log_org_at", t, ["partner_org_id", sa.text("at DESC")])
    op.create_index("ix_partner_org_audit_log_account_at", t, ["account_id", sa.text("at DESC")])

    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_constraint(op.f("ck_partner_orgs_tax_id_kind"), "partner_orgs", type_="check")
    for column in reversed(ORG_COLUMNS):
        op.drop_column("partner_orgs", column)
