# ruff: noqa: E501
"""Epic 42 story 42.4 (FR-216, AD-50, AD-23, AD-24): ``referral_rewards``.

One reward per referral (UNIQUE); ``pending_condition -> eligible -> approved* (human only, approval engine) ->
paid`` or ``void``. Worker referrers are paid through the next Payroll Export (posting source ``referral_reward``,
``payout_channel='payroll'`` with the referrer's payroll Placement), everyone else through the payout export.
Worker org RLS on the denormalised ``referrer_candidate_id``.

Revision ID: 20261006_306_03_ref_rewards
Revises: 20261006_306_02_referrals
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261006_306_03_ref_rewards"
down_revision: str | tuple[str, ...] | None = "20261006_306_02_referrals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFERRER_CHECK = (
    "(referrer_kind = 'user' AND referrer_user_id IS NOT NULL AND referrer_candidate_id IS NULL) OR "
    "(referrer_kind = 'worker' AND referrer_candidate_id IS NOT NULL AND referrer_user_id IS NULL)"
)


def _fk(name: str, col: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete="RESTRICT"
    )


def upgrade() -> None:
    t = "referral_rewards"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("referral_id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("referrer_kind", sa.String(10), nullable=False),
        sa.Column("referrer_user_id", sa.Uuid(), nullable=True),
        sa.Column("referrer_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending_condition'"), nullable=False),
        sa.Column("condition_due_on", sa.Date(), nullable=True),
        sa.Column("eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payout_channel", sa.String(15), nullable=False),
        sa.Column("payee_placement_id", sa.Uuid(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payout_ref", sa.String(100), nullable=True),
        sa.Column("void_reason", sa.String(30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_referral_rewards"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_referral_rewards_tenant_id_id"),
        sa.UniqueConstraint("referral_id", name="uq_referral_rewards_referral_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_referral_rewards_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('pending_condition', 'eligible', 'approved', 'paid', 'void')",
            name=op.f("ck_referral_rewards_status"),
        ),
        sa.CheckConstraint(
            "payout_channel IN ('payroll', 'payout_export')", name=op.f("ck_referral_rewards_payout_channel")
        ),
        sa.CheckConstraint(
            "payout_channel <> 'payroll' OR payee_placement_id IS NOT NULL",
            name=op.f("ck_referral_rewards_payroll_payee"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_referral_rewards_amount")),
        sa.CheckConstraint(
            "referrer_kind IN ('user', 'worker')", name=op.f("ck_referral_rewards_referrer_kind")
        ),
        sa.CheckConstraint(REFERRER_CHECK, name=op.f("ck_referral_rewards_referrer")),
        sa.CheckConstraint(
            "void_reason IS NULL OR void_reason IN ('placement_dropped', 'condition_not_met', 'rejected', 'manual')",
            name=op.f("ck_referral_rewards_void_reason"),
        ),
        _fk("fk_referral_rewards_referral_id", "referral_id", "referrals"),
        _fk("fk_referral_rewards_program_id", "program_id", "referral_programs"),
        _fk("fk_referral_rewards_placement_id", "placement_id", "placements"),
        _fk("fk_referral_rewards_payee_placement", "payee_placement_id", "placements"),
        _fk("fk_referral_rewards_referrer_user_id", "referrer_user_id", "users"),
        _fk("fk_referral_rewards_referrer_cand", "referrer_candidate_id", "candidates"),
    )
    op.create_index("ix_referral_rewards_tenant_id", t, ["tenant_id"])
    op.create_index("ix_referral_rewards_status", t, ["status", "payout_channel"])
    op.create_index("ix_referral_rewards_placement_id", t, ["placement_id"])
    op.create_index("ix_referral_rewards_referrer_cand", t, ["referrer_candidate_id"])
    enable_rls(t)
    enable_org_rls(t, worker_col="referrer_candidate_id")


def downgrade() -> None:
    disable_org_rls("referral_rewards")
    disable_rls("referral_rewards")
    op.drop_table("referral_rewards")
