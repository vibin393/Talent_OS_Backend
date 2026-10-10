# ruff: noqa: E501
"""Epic 32 (story 19.3, AD-31): workflow v2 graphs, immutable rule versions and waits.

* ``workflow_rules`` += ``graph`` (JSONB NULL -- NULL = the Phase 1 form, run by the unchanged P1 engine) and
  ``definition_version`` (1 = P1 shape, 2 = graph).
* New ``workflow_rule_versions`` (immutable, unique ``(rule_id, version)``), backfilled with one row per existing
  rule (its current version). Runs and waits pin ``rule_version``.
* New ``workflow_waits`` (delay / approval waits of graph runs), resumed by ``workflow.resume_waits``.
* ``workflow_executions``: ``status`` += ``waiting`` / ``cancelled``; += ``node_trace``.

Revision ID: 20261003_219_03_workflow_graph
Revises: 20261003_219_02_sla_escalations
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261003_219_03_workflow_graph"
down_revision: str | tuple[str, ...] | None = "20261003_219_02_sla_escalations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

P1_STATUSES = (
    "matched",
    "not_matched",
    "succeeded",
    "partially_failed",
    "failed",
    "skipped_rate_limit",
    "skipped_loop",
)
P2_STATUSES = (*P1_STATUSES, "waiting", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _jsonb(name: str, default: str, *, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(default), nullable=nullable
    )


def upgrade() -> None:
    op.add_column(
        "workflow_rules", sa.Column("graph", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "workflow_rules",
        sa.Column("definition_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_workflow_rules_definition_version"),
        "workflow_rules",
        "(definition_version = 1 AND graph IS NULL) OR (definition_version = 2 AND graph IS NOT NULL)",
    )

    op.create_table(
        "workflow_rule_versions",
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        _jsonb("conditions", "'{}'::jsonb"),
        _jsonb("actions", "'[]'::jsonb"),
        sa.Column("graph", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["workflow_rules.id"],
            name=op.f("fk_workflow_rule_versions_rule_id_workflow_rules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_workflow_rule_versions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_rule_versions")),
        sa.UniqueConstraint("rule_id", "version", name=op.f("uq_workflow_rule_versions_rule_id")),
    )
    op.create_index(op.f("ix_workflow_rule_versions_tenant_id"), "workflow_rule_versions", ["tenant_id"])
    # Backfill before RLS is forced (the owner role would otherwise need a tenant context).
    op.execute(
        "INSERT INTO workflow_rule_versions (id, tenant_id, rule_id, version, trigger, conditions, actions, graph, created_at) "
        "SELECT gen_random_uuid(), tenant_id, id, version, trigger, conditions, actions, NULL, updated_at FROM workflow_rules"
    )
    enable_rls("workflow_rule_versions")
    enable_org_rls("workflow_rule_versions")

    op.drop_constraint(op.f("ck_workflow_executions_status"), "workflow_executions", type_="check")
    op.create_check_constraint(
        op.f("ck_workflow_executions_status"), "workflow_executions", _in("status", P2_STATUSES)
    )
    op.add_column("workflow_executions", _jsonb("node_trace", "'[]'::jsonb"))

    op.create_table(
        "workflow_waits",
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("resume_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("continued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("chain_depth", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _jsonb("extras", "'{}'::jsonb"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_in("kind", ("delay", "approval")), name=op.f("ck_workflow_waits_kind")),
        sa.CheckConstraint(
            _in("status", ("waiting", "resumed", "cancelled", "timed_out")),
            name=op.f("ck_workflow_waits_status"),
        ),
        sa.CheckConstraint(
            "kind <> 'delay' OR resume_at IS NOT NULL", name=op.f("ck_workflow_waits_delay_resume")
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["workflow_executions.id"],
            name=op.f("fk_workflow_waits_execution_id_workflow_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["workflow_rules.id"],
            name=op.f("fk_workflow_waits_rule_id_workflow_rules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_request_id"],
            ["approval_requests.id"],
            name=op.f("fk_workflow_waits_approval_request_id_approval_requests"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_workflow_waits_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_waits")),
    )
    op.create_index(op.f("ix_workflow_waits_tenant_id"), "workflow_waits", ["tenant_id"])
    op.create_index("ix_workflow_waits_status_resume", "workflow_waits", ["tenant_id", "status", "resume_at"])
    op.create_index(
        "ix_workflow_waits_timeout",
        "workflow_waits",
        ["timeout_at"],
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.create_index("ix_workflow_waits_rule", "workflow_waits", ["rule_id", "status"])
    op.create_index("ix_workflow_waits_execution", "workflow_waits", ["execution_id"])
    enable_rls("workflow_waits")
    enable_org_rls("workflow_waits")


def downgrade() -> None:
    disable_org_rls("workflow_waits")
    disable_rls("workflow_waits")
    op.drop_table("workflow_waits")

    op.drop_column("workflow_executions", "node_trace")
    op.execute("UPDATE workflow_executions SET status = 'failed' WHERE status IN ('waiting', 'cancelled')")
    op.drop_constraint(op.f("ck_workflow_executions_status"), "workflow_executions", type_="check")
    op.create_check_constraint(
        op.f("ck_workflow_executions_status"), "workflow_executions", _in("status", P1_STATUSES)
    )

    disable_org_rls("workflow_rule_versions")
    disable_rls("workflow_rule_versions")
    op.drop_table("workflow_rule_versions")

    # Graph rules cannot run on the P1 engine: archive them (their definition is lost with the column).
    op.execute(
        "UPDATE workflow_rules SET is_active = false, archived_at = coalesce(archived_at, now()) WHERE graph IS NOT NULL"
    )
    op.drop_constraint(op.f("ck_workflow_rules_definition_version"), "workflow_rules", type_="check")
    op.drop_column("workflow_rules", "definition_version")
    op.drop_column("workflow_rules", "graph")
