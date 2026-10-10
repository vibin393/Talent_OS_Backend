# ruff: noqa: E501
"""Phase 2 foundation (Epic 13, story 13.6): approval engine v2 (AD-24, FR-170, FR-181).

* ``approval_chains`` / ``approval_requests``: subject-type CHECKs dropped (registry-validated), VARCHAR(40).
* ``approval_chain_steps``: ``approver_kind`` (backfilled ``user`` / ``role``) + ``approver_ref``; the
  ``one_approver`` CHECK only binds the ``user`` / ``role`` kinds.
* ``approval_requests``: ``steps_source``, ``current_approver_kind``, ``current_approver_client_user_id``,
  ``current_step_activated_at``, ``initiated_by_kind``.
* ``approval_decisions``: the full ``ActorRef`` (``actor_kind`` default ``user``, attribution, delegation,
  offline evidence, secure link).
* New ``escalation_policies`` (stored only; Epic 32 evaluates them).
* Org RLS: requests / decisions through ``org_grants`` (``approval_request``); chains, steps and escalation
  policies internal-only.

Revision ID: 20261002_200_04
Revises: 20261002_200_03
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import GrantsRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261002_200_04"
down_revision: str | tuple[str, ...] | None = "20261002_200_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

P1_SUBJECT_TYPES = ("demand", "offer", "margin_override")
ONE_APPROVER_V2 = (
    "(approver_kind = 'user' AND approver_user_id IS NOT NULL) "
    "OR (approver_kind = 'role' AND approver_role_id IS NOT NULL) "
    "OR approver_kind NOT IN ('user', 'role')"
)
ONE_APPROVER_V1 = "(approver_user_id IS NULL) <> (approver_role_id IS NULL)"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # Subject types become registry-validated.
    op.drop_constraint(op.f("ck_approval_chains_subject_type"), "approval_chains", type_="check")
    op.drop_constraint(op.f("ck_approval_requests_subject_type"), "approval_requests", type_="check")
    for table in ("approval_chains", "approval_requests"):
        op.alter_column(
            table,
            "subject_type",
            existing_type=sa.VARCHAR(length=30),
            type_=sa.String(length=40),
            existing_nullable=False,
        )

    # Chain steps: approver kind + reference.
    op.add_column("approval_chain_steps", sa.Column("approver_kind", sa.String(length=32), nullable=True))
    op.add_column(
        "approval_chain_steps",
        sa.Column("approver_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        "UPDATE approval_chain_steps SET approver_kind = CASE WHEN approver_user_id IS NOT NULL THEN 'user' ELSE 'role' END"
    )
    op.alter_column(
        "approval_chain_steps", "approver_kind", existing_type=sa.String(length=32), nullable=False
    )
    op.drop_constraint(op.f("ck_approval_chain_steps_one_approver"), "approval_chain_steps", type_="check")
    op.create_check_constraint(
        op.f("ck_approval_chain_steps_one_approver"), "approval_chain_steps", ONE_APPROVER_V2
    )

    # Requests.
    op.add_column(
        "approval_requests",
        sa.Column("steps_source", sa.String(length=10), server_default=sa.text("'chain'"), nullable=False),
    )
    op.add_column(
        "approval_requests", sa.Column("current_approver_kind", sa.String(length=32), nullable=True)
    )
    op.add_column("approval_requests", sa.Column("current_approver_client_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "approval_requests", sa.Column("current_step_activated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("approval_requests", sa.Column("initiated_by_kind", sa.String(length=20), nullable=True))
    op.create_check_constraint(
        op.f("ck_approval_requests_steps_source"),
        "approval_requests",
        _in("steps_source", ("chain", "explicit")),
    )
    op.execute(
        "UPDATE approval_requests SET current_approver_kind = CASE "
        "WHEN current_approver_user_id IS NOT NULL THEN 'user' "
        "WHEN current_approver_role_id IS NOT NULL THEN 'role' END "
        "WHERE status = 'pending'"
    )
    op.create_index(
        "ix_approval_requests_pending_client_user",
        "approval_requests",
        ["current_approver_client_user_id", "status"],
        unique=False,
    )

    # Decisions: the full ActorRef.
    op.add_column(
        "approval_decisions",
        sa.Column("actor_kind", sa.String(length=20), server_default=sa.text("'user'"), nullable=False),
    )
    op.add_column("approval_decisions", sa.Column("attributed_kind", sa.String(length=20), nullable=True))
    op.add_column("approval_decisions", sa.Column("attributed_id", sa.Uuid(), nullable=True))
    op.add_column("approval_decisions", sa.Column("on_behalf_of", sa.Uuid(), nullable=True))
    op.add_column("approval_decisions", sa.Column("evidence_file_id", sa.Uuid(), nullable=True))
    op.add_column("approval_decisions", sa.Column("secure_link_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE approval_decisions SET actor_kind = 'system' WHERE outcome = 'skipped'")
    op.create_foreign_key(
        "fk_approval_decisions_evidence_file_id_stored_files",
        "approval_decisions",
        "stored_files",
        ["tenant_id", "evidence_file_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "escalation_policies",
        sa.Column("owner_kind", sa.String(length=30), nullable=False),
        sa.Column("owner_ref", sa.String(length=100), nullable=False),
        sa.Column("after_business_hours", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("target_kind", sa.String(length=30), nullable=False),
        sa.Column("target_ref", sa.Uuid(), nullable=True),
        sa.Column("repeat_every_hours", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("max_repeats", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("action IN ('notify', 'reassign')", name=op.f("ck_escalation_policies_action")),
        sa.CheckConstraint(
            "owner_kind IN ('approval_chain_step', 'workflow_node', 'task_type')",
            name=op.f("ck_escalation_policies_owner_kind"),
        ),
        sa.CheckConstraint(
            "target_kind IN ('user', 'manager_of_assignee', 'role')",
            name=op.f("ck_escalation_policies_target_kind"),
        ),
        sa.CheckConstraint(
            "after_business_hours > 0", name=op.f("ck_escalation_policies_after_business_hours")
        ),
        sa.CheckConstraint("max_repeats >= 0", name=op.f("ck_escalation_policies_max_repeats")),
        sa.CheckConstraint(
            "repeat_every_hours IS NULL OR repeat_every_hours > 0",
            name=op.f("ck_escalation_policies_repeat_every_hours"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_escalation_policies_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_escalation_policies")),
    )
    op.create_index(
        "ix_escalation_policies_owner", "escalation_policies", ["owner_kind", "owner_ref"], unique=False
    )
    op.create_index(
        op.f("ix_escalation_policies_tenant_id"), "escalation_policies", ["tenant_id"], unique=False
    )
    enable_rls("escalation_policies")

    enable_org_rls("approval_requests", grants=GrantsRef("approval_request"))
    enable_org_rls("approval_decisions", grants=GrantsRef("approval_request", id_column="request_id"))
    for table in ("approval_chains", "approval_chain_steps", "escalation_policies"):
        enable_org_rls(table)


def downgrade() -> None:
    for table in (
        "approval_chains",
        "approval_chain_steps",
        "escalation_policies",
        "approval_requests",
        "approval_decisions",
    ):
        disable_org_rls(table)
    disable_rls("escalation_policies")
    op.drop_table("escalation_policies")

    # Phase 2 rows cannot be represented in the Phase 1 shape: drop them.
    p1 = ", ".join(repr(s) for s in P1_SUBJECT_TYPES)
    op.execute(f"DELETE FROM approval_requests WHERE subject_type NOT IN ({p1})")
    op.execute(f"DELETE FROM approval_chains WHERE subject_type NOT IN ({p1})")
    op.execute("DELETE FROM approval_chain_steps WHERE approver_kind NOT IN ('user', 'role')")

    op.drop_constraint(
        "fk_approval_decisions_evidence_file_id_stored_files", "approval_decisions", type_="foreignkey"
    )
    for column in (
        "secure_link_id",
        "evidence_file_id",
        "on_behalf_of",
        "attributed_id",
        "attributed_kind",
        "actor_kind",
    ):
        op.drop_column("approval_decisions", column)

    op.drop_index("ix_approval_requests_pending_client_user", table_name="approval_requests")
    op.drop_constraint(op.f("ck_approval_requests_steps_source"), "approval_requests", type_="check")
    for column in (
        "initiated_by_kind",
        "current_step_activated_at",
        "current_approver_client_user_id",
        "current_approver_kind",
        "steps_source",
    ):
        op.drop_column("approval_requests", column)

    op.drop_constraint(op.f("ck_approval_chain_steps_one_approver"), "approval_chain_steps", type_="check")
    op.create_check_constraint(
        op.f("ck_approval_chain_steps_one_approver"), "approval_chain_steps", ONE_APPROVER_V1
    )
    op.drop_column("approval_chain_steps", "approver_ref")
    op.drop_column("approval_chain_steps", "approver_kind")

    for table in ("approval_chains", "approval_requests"):
        op.alter_column(
            table,
            "subject_type",
            existing_type=sa.String(length=40),
            type_=sa.VARCHAR(length=30),
            existing_nullable=False,
        )
    op.create_check_constraint(
        op.f("ck_approval_requests_subject_type"), "approval_requests", _in("subject_type", P1_SUBJECT_TYPES)
    )
    op.create_check_constraint(
        op.f("ck_approval_chains_subject_type"), "approval_chains", _in("subject_type", P1_SUBJECT_TYPES)
    )
