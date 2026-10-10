# ruff: noqa: E501
"""Epic 32 (story 19.2): escalation policies (library + owned copies) and escalation watches.

* ``escalation_policies`` (foundation table, authoritative): += ``name``, ``after_unit`` (display unit of
  ``after_business_hours``), ``calendar`` (business | calendar), ``notify_original_assignee``, ``is_active``,
  ``archived_at`` and ``source_policy_id`` (the library policy an owned copy was made from). ``owner_kind`` widens
  with ``library`` (reusable policies, ``owner_ref`` NULL); a non-``user``/``role`` target needs no ref.
* New ``escalation_watches``: one per (subject kind, subject, step); evaluated by ``sla.evaluate_escalations``.
  ``spec`` snapshots the policy parameters at watch creation (explicit approval steps have no policy row).
* ``approval_chain_steps.escalation_policy_id`` is NOT added (reconciled contract): chain steps get owned copies
  (``owner_kind='approval_chain_step'``, ``owner_ref='<chain id>:<position>'``).

Revision ID: 20261003_219_02_sla_escalations
Revises: 20261003_219_01_sla_generic
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_219_02_sla_escalations"
down_revision: str | tuple[str, ...] | None = "20261003_219_01_sla_generic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_KINDS_V1 = ("approval_chain_step", "workflow_node", "task_type")
OWNER_KINDS_V2 = (*OWNER_KINDS_V1, "library")
AFTER_UNITS = ("business_hours", "business_days", "hours")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    t = "escalation_policies"
    op.add_column(t, sa.Column("name", sa.String(length=200), nullable=True))
    op.add_column(
        t,
        sa.Column(
            "after_unit", sa.String(length=20), server_default=sa.text("'business_hours'"), nullable=False
        ),
    )
    op.add_column(
        t, sa.Column("calendar", sa.String(length=10), server_default=sa.text("'business'"), nullable=False)
    )
    op.add_column(
        t, sa.Column("notify_original_assignee", sa.Boolean(), server_default=sa.text("true"), nullable=False)
    )
    op.add_column(t, sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.add_column(t, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("source_policy_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_escalation_policies_source_policy_id_escalation_policies"),
        t,
        t,
        ["source_policy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(t, "owner_ref", existing_type=sa.String(length=100), nullable=True)
    op.drop_constraint(op.f("ck_escalation_policies_owner_kind"), t, type_="check")
    op.create_check_constraint(
        op.f("ck_escalation_policies_owner_kind"), t, _in("owner_kind", OWNER_KINDS_V2)
    )
    op.create_check_constraint(
        op.f("ck_escalation_policies_owner_ref"), t, "(owner_kind = 'library') = (owner_ref IS NULL)"
    )
    op.create_check_constraint(op.f("ck_escalation_policies_after_unit"), t, _in("after_unit", AFTER_UNITS))
    op.create_check_constraint(
        op.f("ck_escalation_policies_calendar"), t, _in("calendar", ("business", "calendar"))
    )
    # NOT VALID: foundation-era rows are not re-checked; every new / updated row is.
    op.execute(
        "ALTER TABLE escalation_policies ADD CONSTRAINT ck_escalation_policies_target_ref "
        "CHECK (target_kind = 'manager_of_assignee' OR target_ref IS NOT NULL) NOT VALID"
    )
    op.create_index("ix_escalation_policies_source", t, ["source_policy_id"])

    op.create_table(
        "escalation_watches",
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject_kind", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("step_position", sa.Integer(), nullable=True),
        sa.Column("assignee_ref", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fires", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("last_outcome", sa.String(length=40), nullable=True),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_in("subject_kind", ("approval_step", "task")), name=op.f("ck_escalation_watches_subject_kind")),
        sa.CheckConstraint(_in("status", ("active", "done", "cancelled")), name=op.f("ck_escalation_watches_status")),
        sa.CheckConstraint("fires >= 0", name=op.f("ck_escalation_watches_fires")),
        sa.ForeignKeyConstraint(["policy_id"], ["escalation_policies.id"], name=op.f("fk_escalation_watches_policy_id_escalation_policies"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_escalation_watches_tenant_id_tenants"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_escalation_watches")),
        sa.UniqueConstraint(
            "tenant_id", "subject_kind", "subject_id", "step_position",
            name="uq_escalation_watches_subject_step", postgresql_nulls_not_distinct=True,
        ),
    )  # fmt: skip
    op.create_index(op.f("ix_escalation_watches_tenant_id"), "escalation_watches", ["tenant_id"])
    op.create_index("ix_escalation_watches_policy", "escalation_watches", ["policy_id"])
    op.create_index(
        "ix_escalation_watches_due",
        "escalation_watches",
        ["next_fire_at"],
        postgresql_where=sa.text("status = 'active'"),
    )
    enable_rls("escalation_watches")
    enable_org_rls("escalation_watches")


def downgrade() -> None:
    disable_org_rls("escalation_watches")
    disable_rls("escalation_watches")
    op.drop_table("escalation_watches")

    t = "escalation_policies"
    op.execute("DELETE FROM escalation_policies WHERE owner_kind = 'library'")
    op.drop_index("ix_escalation_policies_source", table_name=t)
    for name in ("target_ref", "calendar", "after_unit", "owner_ref", "owner_kind"):
        op.drop_constraint(op.f(f"ck_escalation_policies_{name}"), t, type_="check")
    op.create_check_constraint(
        op.f("ck_escalation_policies_owner_kind"), t, _in("owner_kind", OWNER_KINDS_V1)
    )
    op.alter_column(t, "owner_ref", existing_type=sa.String(length=100), nullable=False)
    op.drop_constraint(
        op.f("fk_escalation_policies_source_policy_id_escalation_policies"), t, type_="foreignkey"
    )
    for column in (
        "source_policy_id",
        "archived_at",
        "is_active",
        "notify_original_assignee",
        "calendar",
        "after_unit",
        "name",
    ):
        op.drop_column(t, column)
