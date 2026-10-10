# ruff: noqa: E501
"""Phase 4 foundation (Epic 53, story 53.1/53.2): the eight agent runtime tables (spec 0.2, AD-58..AD-62).

``agent_principals``, ``agent_configs``, ``agent_action_policies``, ``agent_runs``, ``agent_run_steps``,
``agent_proposals``, ``agent_tool_calls``, ``agent_payloads`` (encrypted store). Tenant RLS on each plus the
internal-only org policy (external principals see nothing). ``agent_runs.conversation_id`` has no FK (the owner of the
conversation table adds it). Proposal human ids use the lazily created sequence key ``agent_proposal`` (no DDL).

Revision ID: 20261008_400_02_p4_runtime
Revises: 20261008_400_01_p4_checks
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_400_02_p4_runtime"
down_revision: str | tuple[str, ...] | None = "20261008_400_01_p4_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "agent_principals",
    "agent_configs",
    "agent_action_policies",
    "agent_runs",
    "agent_run_steps",
    "agent_proposals",
    "agent_tool_calls",
    "agent_payloads",
)
MODES = ("off", "suggest", "auto")
RUN_STATUSES = ("queued", "running", "waiting", "completed", "stopped", "failed", "cancelled")
TRIGGER_KINDS = ("event", "schedule", "workflow", "manual", "copilot", "inbound")
REASON_CODES = (
    "cap_steps", "cap_tool_calls", "cap_tokens", "budget", "kill_switch", "provider_error", "policy_violation",
    "consent_missing", "stale_data", "owner_unavailable", "timeout", "refused", "subject_closed", "manual",
    "alternative_process",
)  # fmt: skip
OUTCOMES = (
    "executed",
    "proposed",
    "refused",
    "policy_violation",
    "invalid",
    "failed",
    "not_found",
    "deduplicated",
)
PROPOSAL_STATUSES = (
    "pending",
    "approved",
    "executed",
    "execution_failed",
    "rejected",
    "expired",
    "stale",
    "discarded",
)
PAYLOAD_KINDS = ("request_delta", "response", "tool_args", "tool_result", "kickoff")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def _uuid_array(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False
    )


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False)


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    t = "agent_principals"
    op.create_table(
        t,
        *_base(t),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("spec_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.UniqueConstraint("tenant_id", "agent_key", name="uq_agent_principals_tenant_id_agent_key"),
        _ck(t, "status", "status IN ('active', 'disabled')"),
    )

    t = "agent_configs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("default_mode", sa.String(8), nullable=True),
        sa.Column("monthly_token_budget", sa.BigInteger(), nullable=True),
        _jsonb("limits", "{}"),
        _jsonb("triggers_enabled", "{}"),
        sa.Column("channels", postgresql.ARRAY(sa.String(20)), nullable=True),
        sa.Column("working_hours", postgresql.JSONB(), nullable=True),
        sa.Column("proposal_expiry_hours", sa.Integer(), nullable=True),
        _ts("burn_in_until"),
        _ts("enabled_at"),
        sa.Column("enabled_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "agent_key", name="uq_agent_configs_tenant_id_agent_key"),
        _fk(t, "enabled_by", "users"),
        _ck(t, "default_mode", f"default_mode IS NULL OR {_in('default_mode', MODES)}"),
        _ck(t, "budget", "monthly_token_budget IS NULL OR monthly_token_budget >= 0"),
        _ck(t, "proposal_expiry", "proposal_expiry_hours IS NULL OR proposal_expiry_hours BETWEEN 1 AND 720"),
    )

    t = "agent_action_policies"
    op.create_table(
        t,
        *_base(t),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("tool_key", sa.String(80), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("route_role_key", sa.String(60), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "agent_key", "tool_key", name="uq_agent_action_policies_tenant_id_agent_key_tool_key"
        ),
        _fk(t, "updated_by", "users"),
        _ck(t, "mode", _in("mode", MODES)),
    )

    t = "agent_runs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("agent_principal_id", sa.Uuid(), nullable=False),
        sa.Column("spec_version", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(12), nullable=False),
        sa.Column("trigger_kind", sa.String(10), nullable=False),
        sa.Column("trigger_key", sa.String(80), nullable=True),
        sa.Column("dedup_key", sa.String(200), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_by_kind", sa.String(20), nullable=False),
        sa.Column("started_by_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.String(40), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        _uuid_array("candidate_ids"),
        _jsonb("related_refs", "[]"),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("wait_condition", sa.String(40), nullable=True),
        sa.Column("wait_ref", sa.Uuid(), nullable=True),
        _ts("wait_until"),
        sa.Column("model_tier", sa.String(10), nullable=False),
        sa.Column("prompt_ref", sa.String(120), nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("next_step_no", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _count("step_count"),
        _count("tool_call_count"),
        _count("write_count"),
        _count("retry_count"),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tainted", sa.Boolean(), server_default=sa.false(), nullable=False),
        _count("policy_violations"),
        sa.Column("lease_owner", sa.String(80), nullable=True),
        _ts("lease_expires_at"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("fallback_task_id", sa.Uuid(), nullable=True),
        _fk(t, "agent_principal_id", "agent_principals"),
        _fk(t, "owner_user_id", "users"),
        _fk(t, "parent_run_id", "agent_runs"),
        _ck(t, "origin", "origin IN ('agent', 'copilot', 'conversation')"),
        _ck(t, "trigger_kind", _in("trigger_kind", TRIGGER_KINDS)),
        _ck(t, "status", _in("status", RUN_STATUSES)),
        _ck(t, "reason_code", f"reason_code IS NULL OR {_in('reason_code', REASON_CODES)}"),
        _ck(t, "subject_pair", "(subject_type IS NULL) = (subject_id IS NULL)"),
        _ck(t, "waiting_condition", "status <> 'waiting' OR wait_condition IS NOT NULL"),
    )
    op.create_index(
        "uq_agent_runs_dedup", t, ["tenant_id", "agent_key", "dedup_key"], unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status IN ('queued', 'running', 'waiting')"),
    )  # fmt: skip
    op.create_index("ix_agent_runs_tenant_id_status", t, ["tenant_id", "status"])
    op.create_index("ix_agent_runs_subject", t, ["subject_type", "subject_id"])
    op.create_index("ix_agent_runs_agent_key_created_at", t, ["agent_key", "created_at"])
    op.create_index("ix_agent_runs_owner_user_id_created_at", t, ["owner_user_id", "created_at"])
    op.create_index("ix_agent_runs_conversation_id", t, ["conversation_id"])
    op.create_index(
        "ix_agent_runs_wait",
        t,
        ["wait_condition", "wait_ref"],
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.create_index("ix_agent_runs_candidate_ids", t, ["candidate_ids"], postgresql_using="gin")
    op.create_index(
        "ix_agent_runs_lease_expires_at",
        t,
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    t = "agent_run_steps"
    op.create_table(
        t,
        *_base(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("stop_reason", sa.String(20), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("request_sha256", sa.String(64), nullable=True),
        sa.Column("response_sha256", sa.String(64), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "step_no", name="uq_agent_run_steps_tenant_id_run_id_step_no"
        ),
        _fk(t, "run_id", "agent_runs", ondelete="CASCADE"),
        _ck(t, "status", "status IN ('ok', 'failed', 'dropped')"),
    )

    t = "agent_proposals"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("tool_key", sa.String(80), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("args_sha256", sa.String(64), nullable=False),
        sa.Column("preview", postgresql.JSONB(), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("target_version", sa.String(100), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        _jsonb("evidence", "[]"),
        sa.Column("risk", sa.String(10), nullable=False),
        _uuid_array("candidate_ids"),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("edited", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("assignee_user_id", sa.Uuid(), nullable=True),
        sa.Column("route_role_key", sa.String(60), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        _ts("decided_at"),
        sa.Column("decision_note", sa.String(1000), nullable=True),
        _ts("executed_at"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_agent_proposals_tenant_id_human_id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_agent_proposals_tenant_id_idempotency_key"
        ),
        _fk(t, "run_id", "agent_runs"),
        _fk(t, "assignee_user_id", "users"),
        _fk(t, "owner_user_id", "users"),
        _fk(t, "decided_by", "users"),
        _ck(t, "origin", "origin IN ('agent', 'copilot')"),
        _ck(t, "status", _in("status", PROPOSAL_STATUSES)),
        _ck(t, "risk", "risk IN ('low', 'high')"),
        _ck(t, "routed", "assignee_user_id IS NOT NULL OR route_role_key IS NOT NULL"),
        _ck(t, "confidence", "confidence IS NULL OR confidence BETWEEN 0 AND 1"),
    )
    op.create_index("ix_agent_proposals_status_assignee", t, ["tenant_id", "status", "assignee_user_id"])
    op.create_index("ix_agent_proposals_route_role_key_status", t, ["route_role_key", "status"])
    op.create_index("ix_agent_proposals_target", t, ["target_type", "target_id"])
    op.create_index("ix_agent_proposals_run_id", t, ["run_id"])
    op.create_index("ix_agent_proposals_candidate_ids", t, ["candidate_ids"], postgresql_using="gin")

    t = "agent_tool_calls"
    op.create_table(
        t,
        *_base(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("tool_use_id", sa.String(64), nullable=False),
        sa.Column("tool_key", sa.String(80), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("risk", sa.String(10), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("mode_reason", sa.String(40), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("args_redacted", postgresql.JSONB(), nullable=True),
        sa.Column("result_summary", sa.String(500), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("egress", sa.Boolean(), nullable=False),
        _uuid_array("candidate_ids"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("executed_as", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "tool_use_id", name="uq_agent_tool_calls_tenant_id_run_id_tool_use_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_agent_tool_calls_tenant_id_idempotency_key"
        ),
        _fk(t, "run_id", "agent_runs", ondelete="CASCADE"),
        _fk(t, "step_id", "agent_run_steps", ondelete="CASCADE"),
        _fk(t, "proposal_id", "agent_proposals"),
        _ck(t, "risk", "risk IN ('read', 'low', 'high')"),
        _ck(t, "mode", _in("mode", MODES)),
        _ck(t, "outcome", _in("outcome", OUTCOMES)),
    )
    op.create_index("ix_agent_tool_calls_tool_key_created_at", t, ["tool_key", "created_at"])
    op.create_index(
        "ix_agent_tool_calls_violations",
        t,
        ["outcome"],
        postgresql_where=sa.text("outcome = 'policy_violation'"),
    )
    op.create_index("ix_agent_tool_calls_candidate_ids", t, ["candidate_ids"], postgresql_using="gin")

    t = "agent_payloads"
    op.create_table(
        t,
        *_base(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.Uuid(), nullable=True),
        sa.Column("step_no", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _fk(t, "run_id", "agent_runs", ondelete="CASCADE"),
        _fk(t, "step_id", "agent_run_steps", ondelete="CASCADE"),
        _fk(t, "tool_call_id", "agent_tool_calls", ondelete="CASCADE"),
        _ck(t, "kind", _in("kind", PAYLOAD_KINDS)),
    )
    op.create_index("ix_agent_payloads_run_id_kind", t, ["run_id", "kind"])
    op.create_index("ix_agent_payloads_expires_at", t, ["expires_at"])

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
