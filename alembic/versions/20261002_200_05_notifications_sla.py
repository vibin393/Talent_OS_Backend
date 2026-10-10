"""Phase 2 foundation (Epic 13, story 13.7): notification recipients, generic SLA clocks, system tasks.

* ``notifications``: ``recipient_kind`` / ``recipient_id`` (backfilled from ``user_id``), org columns
  (``client_id`` / ``vendor_id`` / ``candidate_id``), ``user_id`` NULL-able for non-user recipients; dedupe is
  per recipient. ``notification_deliveries`` inherit the org columns from their notification. Org RLS on both;
  ``notification_preferences`` / ``notification_settings`` are internal-only.
* ``sla_targets`` (Phase 1 table, R14): ``subject_type`` (default ``demand``), ``scope``, ``calendar``,
  ``is_active``; the milestone CHECK binds demand rows only; the unique key includes subject type and scope.
* New ``sla_clocks`` (generic clocks). Both SLA tables are internal-only.
* ``tasks`` (R7): ``created_by`` NULL-able (system tasks), ``source`` widened.

Revision ID: 20261002_200_05
Revises: 20261002_200_04
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261002_200_05"
down_revision: str | tuple[str, ...] | None = "20261002_200_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECIPIENT_KINDS = ("user", "candidate", "client_user", "vendor_user")
DEMAND_MILESTONES = ("assignment", "first_submission", "first_interview", "client_feedback")
P1_TASK_SOURCES = ("manual", "workflow")
P2_TASK_SOURCES = (*P1_TASK_SOURCES, "onboarding", "offboarding", "collections", "client_portal", "bgv")
ORG_COLUMNS = ("client_id", "vendor_id", "candidate_id")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # ---------------------------------------------------------------- notifications
    op.add_column(
        "notifications",
        sa.Column("recipient_kind", sa.String(length=16), server_default=sa.text("'user'"), nullable=False),
    )
    op.add_column("notifications", sa.Column("recipient_id", sa.Uuid(), nullable=True))
    for column in ORG_COLUMNS:
        op.add_column("notifications", sa.Column(column, sa.Uuid(), nullable=True))
        op.add_column("notification_deliveries", sa.Column(column, sa.Uuid(), nullable=True))
    op.execute("UPDATE notifications SET recipient_id = user_id WHERE recipient_id IS NULL")
    op.alter_column("notifications", "recipient_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("notifications", "user_id", existing_type=sa.UUID(), nullable=True)
    op.drop_constraint("uq_notifications_tenant_id_user_id_dedupe_key", "notifications", type_="unique")
    op.create_unique_constraint(
        "uq_notifications_recipient_dedupe",
        "notifications",
        ["tenant_id", "recipient_kind", "recipient_id", "dedupe_key"],
    )
    op.create_index(
        "ix_notifications_recipient_created",
        "notifications",
        ["tenant_id", "recipient_kind", "recipient_id", "created_at"],
        unique=False,
    )
    op.create_check_constraint(
        op.f("ck_notifications_recipient_kind"), "notifications", _in("recipient_kind", RECIPIENT_KINDS)
    )
    op.create_check_constraint(
        op.f("ck_notifications_user_recipient"),
        "notifications",
        "recipient_kind <> 'user' OR user_id = recipient_id",
    )
    op.create_check_constraint(
        op.f("ck_notifications_user_id_for_users"),
        "notifications",
        "(recipient_kind = 'user') = (user_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_notifications_org_columns"),
        "notifications",
        "((client_id IS NOT NULL) = (recipient_kind = 'client_user')) "
        "AND ((vendor_id IS NOT NULL) = (recipient_kind = 'vendor_user')) "
        "AND ((candidate_id IS NOT NULL) = (recipient_kind = 'candidate'))",
    )
    enable_org_rls("notifications", client_col="client_id", vendor_col="vendor_id", worker_col="candidate_id")
    enable_org_rls(
        "notification_deliveries",
        client_col="client_id",
        vendor_col="vendor_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("notifications", "notification_id", ORG_COLUMNS),
    )
    enable_org_rls("notification_preferences")
    enable_org_rls("notification_settings")

    # ---------------------------------------------------------------- sla_targets (Phase 1 table, R14)
    op.add_column(
        "sla_targets",
        sa.Column("subject_type", sa.String(length=40), server_default=sa.text("'demand'"), nullable=False),
    )
    op.add_column(
        "sla_targets",
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "sla_targets",
        sa.Column("calendar", sa.String(length=10), server_default=sa.text("'business'"), nullable=False),
    )
    op.add_column(
        "sla_targets", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False)
    )
    op.execute("UPDATE sla_targets SET calendar = clock")
    op.drop_constraint("uq_sla_targets_tenant_id_milestone_priority", "sla_targets", type_="unique")
    op.create_unique_constraint(
        "uq_sla_targets_subject_milestone_scope",
        "sla_targets",
        ["tenant_id", "subject_type", "milestone", "priority", "scope"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint(op.f("ck_sla_targets_milestone"), "sla_targets", type_="check")
    op.create_check_constraint(
        op.f("ck_sla_targets_milestone"),
        "sla_targets",
        f"subject_type <> 'demand' OR {_in('milestone', DEMAND_MILESTONES)}",
    )
    op.create_check_constraint(
        op.f("ck_sla_targets_calendar"), "sla_targets", _in("calendar", ("business", "calendar"))
    )
    enable_org_rls("sla_targets")

    op.create_table(
        "sla_clocks",
        sa.Column("subject_type", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("milestone", sa.String(length=40), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("target_hours", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("calendar", sa.String(length=10), nullable=False),
        sa.Column("at_risk_percent", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("at_risk_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=10), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('met', 'breached', 'cancelled')",
            name=op.f("ck_sla_clocks_outcome"),
        ),
        sa.CheckConstraint(
            "(completed_at IS NULL) = (outcome IS NULL)", name=op.f("ck_sla_clocks_completed_outcome")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sla_clocks_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sla_clocks")),
    )
    op.create_index(
        "ix_sla_clocks_open_due",
        "sla_clocks",
        ["due_at"],
        unique=False,
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.create_index("ix_sla_clocks_subject", "sla_clocks", ["subject_type", "subject_id"], unique=False)
    op.create_index(op.f("ix_sla_clocks_tenant_id"), "sla_clocks", ["tenant_id"], unique=False)
    op.create_index(
        "uq_sla_clocks_running",
        "sla_clocks",
        [
            "tenant_id",
            "subject_type",
            "subject_id",
            "milestone",
            sa.literal_column("coalesce(ref_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
        ],
        unique=True,
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    enable_rls("sla_clocks")
    enable_org_rls("sla_clocks")

    # ---------------------------------------------------------------- tasks (R7)
    op.alter_column("tasks", "created_by", existing_type=sa.UUID(), nullable=True)
    op.alter_column(
        "tasks",
        "source",
        existing_type=sa.VARCHAR(length=10),
        type_=sa.String(length=20),
        existing_nullable=False,
        existing_server_default=sa.text("'manual'::character varying"),
    )
    op.drop_constraint(op.f("ck_tasks_source"), "tasks", type_="check")
    op.create_check_constraint(op.f("ck_tasks_source"), "tasks", _in("source", P2_TASK_SOURCES))


def downgrade() -> None:
    p1_sources = ", ".join(repr(s) for s in P1_TASK_SOURCES)
    op.execute(f"DELETE FROM tasks WHERE created_by IS NULL OR source NOT IN ({p1_sources})")
    op.drop_constraint(op.f("ck_tasks_source"), "tasks", type_="check")
    op.create_check_constraint(op.f("ck_tasks_source"), "tasks", _in("source", P1_TASK_SOURCES))
    op.alter_column(
        "tasks",
        "source",
        existing_type=sa.String(length=20),
        type_=sa.VARCHAR(length=10),
        existing_nullable=False,
        existing_server_default=sa.text("'manual'::character varying"),
    )
    op.alter_column("tasks", "created_by", existing_type=sa.UUID(), nullable=False)

    disable_org_rls("sla_clocks")
    disable_rls("sla_clocks")
    op.drop_table("sla_clocks")

    disable_org_rls("sla_targets")
    op.execute("DELETE FROM sla_targets WHERE subject_type <> 'demand' OR scope <> '{}'::jsonb")
    op.drop_constraint(op.f("ck_sla_targets_calendar"), "sla_targets", type_="check")
    op.drop_constraint(op.f("ck_sla_targets_milestone"), "sla_targets", type_="check")
    op.create_check_constraint(
        op.f("ck_sla_targets_milestone"), "sla_targets", _in("milestone", DEMAND_MILESTONES)
    )
    op.drop_constraint("uq_sla_targets_subject_milestone_scope", "sla_targets", type_="unique")
    op.create_unique_constraint(
        "uq_sla_targets_tenant_id_milestone_priority",
        "sla_targets",
        ["tenant_id", "milestone", "priority"],
        postgresql_nulls_not_distinct=True,
    )
    for column in ("is_active", "calendar", "scope", "subject_type"):
        op.drop_column("sla_targets", column)

    for table in (
        "notification_settings",
        "notification_preferences",
        "notification_deliveries",
        "notifications",
    ):
        disable_org_rls(table)
    op.execute("DELETE FROM notifications WHERE recipient_kind <> 'user'")
    for name in ("org_columns", "user_id_for_users", "user_recipient", "recipient_kind"):
        op.drop_constraint(op.f(f"ck_notifications_{name}"), "notifications", type_="check")
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
    op.drop_constraint("uq_notifications_recipient_dedupe", "notifications", type_="unique")
    op.create_unique_constraint(
        "uq_notifications_tenant_id_user_id_dedupe_key",
        "notifications",
        ["tenant_id", "user_id", "dedupe_key"],
    )
    op.alter_column("notifications", "user_id", existing_type=sa.UUID(), nullable=False)
    for column in ORG_COLUMNS:
        op.drop_column("notification_deliveries", column)
        op.drop_column("notifications", column)
    op.drop_column("notifications", "recipient_id")
    op.drop_column("notifications", "recipient_kind")
