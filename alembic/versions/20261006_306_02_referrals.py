# ruff: noqa: E501
"""Epic 42 story 42.2 (FR-214, AD-50): ``referral_share_links`` and ``referrals``.

* No ``applications.referral_id`` (foundation 0.3): the pipeline link is ``referrals.application_id`` (UNIQUE)
  plus ``applications.source_ref = str(referral_id)``.
* Partial UNIQUE open referral per (demand, lower(referee email)) -> 409 ``referral_exists``.
* Worker org RLS on ``referrer_candidate_id`` for both tables (a Worker portal session sees only its own rows).

Revision ID: 20261006_306_02_referrals
Revises: 20261006_306_01_ref_programs
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261006_306_02_referrals"
down_revision: str | tuple[str, ...] | None = "20261006_306_01_ref_programs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "submitted",
    "accepted",
    "in_process",
    "hired",
    "not_selected",
    "duplicate",
    "declined",
    "expired",
    "withdrawn",
)
REASONS = ("duplicate", "excluded_referrer", "no_program", "self_referral")
REFERRER_CHECK = (
    "(referrer_kind = 'user' AND referrer_user_id IS NOT NULL AND referrer_candidate_id IS NULL) OR "
    "(referrer_kind = 'worker' AND referrer_candidate_id IS NOT NULL AND referrer_user_id IS NULL)"
)
NIL_UUID = "'00000000-0000-0000-0000-000000000000'::uuid"


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(name: str, col: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete="RESTRICT"
    )


def upgrade() -> None:
    t = "referral_share_links"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("referrer_kind", sa.String(10), nullable=False),
        sa.Column("referrer_user_id", sa.Uuid(), nullable=True),
        sa.Column("referrer_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=False),
        sa.Column("token_sealed", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uses", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            "referrer_kind IN ('user', 'worker')", name=op.f("ck_referral_share_links_referrer_kind")
        ),
        sa.CheckConstraint(REFERRER_CHECK, name=op.f("ck_referral_share_links_referrer")),
        _fk("fk_referral_share_links_user_id", "referrer_user_id", "users"),
        _fk("fk_referral_share_links_candidate_id", "referrer_candidate_id", "candidates"),
        _fk("fk_referral_share_links_demand_id", "demand_id", "demands"),
    )
    op.create_index("ix_referral_share_links_tenant_id", t, ["tenant_id"])
    op.create_index("ix_referral_share_links_candidate", t, ["referrer_candidate_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_referral_share_links_active ON referral_share_links (tenant_id, referrer_kind, "
        f"COALESCE(referrer_user_id, referrer_candidate_id), COALESCE(demand_id, {NIL_UUID})) WHERE revoked_at IS NULL"
    )
    enable_rls(t)
    enable_org_rls(t, worker_col="referrer_candidate_id")

    t = "referrals"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=True),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("referrer_kind", sa.String(10), nullable=False),
        sa.Column("referrer_user_id", sa.Uuid(), nullable=True),
        sa.Column("referrer_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("referee_name", sa.String(200), nullable=True),
        sa.Column("referee_email", postgresql.CITEXT(), nullable=True),
        sa.Column("referee_phone", sa.String(40), nullable=True),
        sa.Column("relationship", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resume_file_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("share_link_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'submitted'"), nullable=False),
        sa.Column("reward_eligible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ineligible_reason", sa.String(30), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_referrals_human_id"),
        sa.UniqueConstraint("application_id", name="uq_referrals_application_id"),
        sa.CheckConstraint("referrer_kind IN ('user', 'worker')", name=op.f("ck_referrals_referrer_kind")),
        sa.CheckConstraint(REFERRER_CHECK, name=op.f("ck_referrals_referrer")),
        sa.CheckConstraint("channel IN ('upload', 'share_link')", name=op.f("ck_referrals_channel")),
        sa.CheckConstraint(_in("status", STATUSES), name=op.f("ck_referrals_status")),
        sa.CheckConstraint(
            f"ineligible_reason IS NULL OR {_in('ineligible_reason', REASONS)}",
            name=op.f("ck_referrals_ineligible_reason"),
        ),
        _fk("fk_referrals_program_id", "program_id", "referral_programs"),
        _fk("fk_referrals_demand_id", "demand_id", "demands"),
        _fk("fk_referrals_referrer_user_id", "referrer_user_id", "users"),
        _fk("fk_referrals_referrer_candidate_id", "referrer_candidate_id", "candidates"),
        _fk("fk_referrals_candidate_id", "candidate_id", "candidates"),
        _fk("fk_referrals_application_id", "application_id", "applications"),
        _fk("fk_referrals_placement_id", "placement_id", "placements"),
        _fk("fk_referrals_resume_file_id", "resume_file_id", "stored_files"),
        _fk("fk_referrals_share_link_id", "share_link_id", "referral_share_links"),
    )
    for name, cols in (
        ("ix_referrals_tenant_id", ["tenant_id"]),
        ("ix_referrals_status", ["status"]),
        ("ix_referrals_demand_id", ["demand_id"]),
        ("ix_referrals_referrer_user_id", ["referrer_user_id"]),
        ("ix_referrals_referrer_candidate_id", ["referrer_candidate_id"]),
        ("ix_referrals_candidate_id", ["candidate_id"]),
        ("ix_referrals_placement_id", ["placement_id"]),
    ):
        op.create_index(name, t, cols)
    op.execute(
        "CREATE UNIQUE INDEX uq_referrals_open_referee ON referrals (tenant_id, demand_id, lower(referee_email::text)) "
        "WHERE demand_id IS NOT NULL AND referee_email IS NOT NULL "
        "AND status NOT IN ('declined', 'expired', 'withdrawn')"
    )
    enable_rls(t)
    enable_org_rls(t, worker_col="referrer_candidate_id")


def downgrade() -> None:
    for t in ("referrals", "referral_share_links"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
