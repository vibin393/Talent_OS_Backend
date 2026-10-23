# ruff: file-ignore[line-too-long]
"""Agent runtime tables (Epic 53, AD-58..AD-62; migration ``20261008_400_02_p4_runtime``). Owner: ``agent_runtime``.

* ``agent_principals`` -- one non-human identity per (tenant, agent key). Rights are computed per run (Effective Rights =
  permission ceiling ∩ Accountable Owner); it cannot sign in, hold API keys or approve.
* ``agent_configs`` / ``agent_action_policies`` -- tenant mode, budgets, caps, triggers, channels, routing per action.
  Enablement itself is the AI feature toggle ``agent_<key>`` (``ai_tenant_settings.features``).
* ``agent_runs`` / ``agent_run_steps`` / ``agent_tool_calls`` -- the run log (metadata, redacted args, summaries only).
* ``agent_payloads`` -- encrypted full content (history replay + governance reads), retention 180 days.
* ``agent_proposals`` -- suggest-mode writes and Copilot Draft Actions waiting for a person (no approvals engine).

Every table is tenant RLS + internal-only org RLS (external principals see nothing).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import AuditedMixin, Base, TenantMixin, Timestamps, UUIDPk

AUTONOMY_MODES = ("off", "suggest", "auto")
PRINCIPAL_STATUSES = ("active", "disabled")
RUN_ORIGINS = ("agent", "copilot", "conversation", "external", "portal")  # Epic 66 (500_01): +external, portal
TRIGGER_KINDS = ("event", "schedule", "workflow", "manual", "copilot", "inbound", "external", "portal")
RUN_STATUSES = ("queued", "running", "waiting", "completed", "stopped", "failed", "cancelled")
ACTIVE_RUN_STATUSES = ("queued", "running", "waiting")
FINAL_RUN_STATUSES = frozenset({"completed", "stopped", "failed", "cancelled"})
REASON_CODES = (
    "cap_steps", "cap_tool_calls", "cap_tokens", "budget", "kill_switch", "provider_error", "policy_violation",
    "consent_missing", "stale_data", "owner_unavailable", "timeout", "refused", "subject_closed", "manual",
    "alternative_process",
)  # fmt: skip
STEP_STATUSES = ("ok", "failed", "dropped")
RISK_CLASSES = ("read", "low", "high")
TOOL_CALL_OUTCOMES = (
    "executed", "proposed", "refused", "policy_violation", "invalid", "failed", "not_found", "deduplicated",
)  # fmt: skip
PAYLOAD_KINDS = ("request_delta", "response", "tool_args", "tool_result", "kickoff")
PROPOSAL_STATUSES = (
    "pending", "approved", "executed", "execution_failed", "rejected", "expired", "stale", "discarded",
)  # fmt: skip
PROPOSAL_RISKS = ("low", "high")
PROPOSAL_ORIGINS = ("agent", "copilot", "external", "portal")  # Epic 66 (500_01)
# Epic 66 (500_04, AD-84/AD-89): run / Proposal owners beyond internal users (portal principals; MCP runs stay ``user``).
OWNER_PRINCIPAL_TYPES = ("user", "client_user", "vendor_user", "candidate")
OWNER_ORG_KINDS = ("client", "vendor", "worker")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _owner_checks() -> tuple[CheckConstraint, ...]:
    """Epic 66 (500_04): the owner-principal CHECKs shared by ``agent_runs`` and ``agent_proposals``."""
    return (
        CheckConstraint(_in("owner_principal_type", OWNER_PRINCIPAL_TYPES), name="owner_principal_type"),
        CheckConstraint(f"owner_org_kind IS NULL OR {_in('owner_org_kind', OWNER_ORG_KINDS)}", name="owner_org_kind"),
        CheckConstraint("(owner_principal_type = 'user') = (owner_user_id IS NOT NULL)", name="owner_user"),
        CheckConstraint("owner_principal_type = 'user' OR owner_org_kind IS NOT NULL", name="owner_org"),
    )


def _tenant_fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _uuid_array() -> Any:
    return mapped_column(ARRAY(Uuid(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]"))


def _jsonb(default: str) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))


def _count() -> Any:
    return mapped_column(Integer, nullable=False, server_default=text("0"))


class AgentPrincipal(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "agent_principals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "agent_key"),
        CheckConstraint(_in("status", PRINCIPAL_STATUSES), name="status"),
    )

    agent_key: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'active'"))


class AgentConfig(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "agent_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "agent_key"),
        _tenant_fk("enabled_by", "users", "fk_agent_configs_enabled_by_users"),
        CheckConstraint(
            f"default_mode IS NULL OR {_in('default_mode', AUTONOMY_MODES)}", name="default_mode"
        ),
        CheckConstraint("monthly_token_budget IS NULL OR monthly_token_budget >= 0", name="budget"),
        CheckConstraint(
            "proposal_expiry_hours IS NULL OR proposal_expiry_hours BETWEEN 1 AND 720", name="proposal_expiry"
        ),
    )

    agent_key: Mapped[str] = mapped_column(String(40), nullable=False)
    default_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    monthly_token_budget: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    limits: Mapped[dict[str, Any]] = _jsonb("{}")
    triggers_enabled: Mapped[dict[str, Any]] = _jsonb("{}")
    channels: Mapped[list[str] | None] = mapped_column(ARRAY(String(20)), nullable=True)
    working_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    proposal_expiry_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    burn_in_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AgentActionPolicy(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "agent_action_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "agent_key", "tool_key"),
        _tenant_fk("updated_by", "users", "fk_agent_action_policies_updated_by_users"),
        CheckConstraint(_in("mode", AUTONOMY_MODES), name="mode"),
    )

    agent_key: Mapped[str] = mapped_column(String(40), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    route_role_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AgentRun(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk(
            "agent_principal_id", "agent_principals", "fk_agent_runs_agent_principal_id_agent_principals"
        ),
        _tenant_fk("owner_user_id", "users", "fk_agent_runs_owner_user_id_users"),
        _tenant_fk("parent_run_id", "agent_runs", "fk_agent_runs_parent_run_id_agent_runs"),
        CheckConstraint(_in("origin", RUN_ORIGINS), name="origin"),
        CheckConstraint(_in("trigger_kind", TRIGGER_KINDS), name="trigger_kind"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint(f"reason_code IS NULL OR {_in('reason_code', REASON_CODES)}", name="reason_code"),
        CheckConstraint("(subject_type IS NULL) = (subject_id IS NULL)", name="subject_pair"),
        CheckConstraint("status <> 'waiting' OR wait_condition IS NOT NULL", name="waiting_condition"),
        Index(
            "uq_agent_runs_dedup",
            "tenant_id",
            "agent_key",
            "dedup_key",
            unique=True,
            postgresql_where=text("dedup_key IS NOT NULL AND status IN ('queued', 'running', 'waiting')"),
        ),
        Index("ix_agent_runs_tenant_id_status", "tenant_id", "status"),
        Index("ix_agent_runs_subject", "subject_type", "subject_id"),
        Index("ix_agent_runs_agent_key_created_at", "agent_key", "created_at"),
        Index("ix_agent_runs_owner_user_id_created_at", "owner_user_id", "created_at"),
        Index("ix_agent_runs_conversation_id", "conversation_id"),
        Index(
            "ix_agent_runs_wait", "wait_condition", "wait_ref", postgresql_where=text("status = 'waiting'")
        ),
        Index("ix_agent_runs_candidate_ids", "candidate_ids", postgresql_using="gin"),
        Index(
            "ix_agent_runs_lease_expires_at", "lease_expires_at", postgresql_where=text("status = 'running'")
        ),
        *_owner_checks(),
        Index("ix_agent_runs_owner_principal", "owner_principal_type", "owner_principal_id", "created_at"),
    )

    agent_key: Mapped[str] = mapped_column(String(40), nullable=False)
    agent_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(12), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    trigger_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Epic 66 (500_04): NULL for portal owners (``owner_principal_type`` <> ``user``).
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_principal_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'user'"))
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_org_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    owner_org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    app_installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    external_client: Mapped[str | None] = mapped_column(String(120), nullable=True)
    started_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    started_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    candidate_ids: Mapped[list[uuid.UUID]] = _uuid_array()
    related_refs: Mapped[list[Any]] = _jsonb("[]")
    # FK added by the owner of the conversation table (54 Copilot, 57 Screening).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'queued'"))
    wait_condition: Mapped[str | None] = mapped_column(String(40), nullable=True)
    wait_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_tier: Mapped[str] = mapped_column(String(10), nullable=False)
    prompt_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    next_step_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    step_count: Mapped[int] = _count()
    tool_call_count: Mapped[int] = _count()
    write_count: Mapped[int] = _count()
    retry_count: Mapped[int] = _count()
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tainted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    policy_violations: Mapped[int] = _count()
    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fallback_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class AgentRunStep(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "agent_run_steps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "step_no"),
        _tenant_fk("run_id", "agent_runs", "fk_agent_run_steps_run_id_agent_runs", ondelete="CASCADE"),
        CheckConstraint(_in("status", STEP_STATUSES), name="status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    ai_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AgentProposal(UUIDPk, TenantMixin, Timestamps, AuditedMixin, Base):
    __tablename__ = "agent_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "human_id"),
        UniqueConstraint("tenant_id", "idempotency_key"),
        _tenant_fk("run_id", "agent_runs", "fk_agent_proposals_run_id_agent_runs"),
        _tenant_fk("assignee_user_id", "users", "fk_agent_proposals_assignee_user_id_users"),
        _tenant_fk("owner_user_id", "users", "fk_agent_proposals_owner_user_id_users"),
        _tenant_fk("decided_by", "users", "fk_agent_proposals_decided_by_users"),
        CheckConstraint(_in("origin", PROPOSAL_ORIGINS), name="origin"),
        CheckConstraint(_in("status", PROPOSAL_STATUSES), name="status"),
        CheckConstraint(_in("risk", PROPOSAL_RISKS), name="risk"),
        CheckConstraint("assignee_user_id IS NOT NULL OR route_role_key IS NOT NULL", name="routed"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence"),
        Index("ix_agent_proposals_status_assignee", "tenant_id", "status", "assignee_user_id"),
        Index("ix_agent_proposals_route_role_key_status", "route_role_key", "status"),
        Index("ix_agent_proposals_target", "target_type", "target_id"),
        Index("ix_agent_proposals_run_id", "run_id"),
        Index("ix_agent_proposals_candidate_ids", "candidate_ids", postgresql_using="gin"),
        *_owner_checks(),
    )
    __audit_exclude__ = ("args", "preview", "result", "evidence", "rationale")

    human_id: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_key: Mapped[str] = mapped_column(String(40), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    args_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    evidence: Mapped[list[Any]] = _jsonb("[]")
    risk: Mapped[str] = mapped_column(String(10), nullable=False)
    candidate_ids: Mapped[list[uuid.UUID]] = _uuid_array()
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    route_role_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Epic 66 (500_04): NULL for portal owners; external (MCP) Proposals carry ``app_installation_id``.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_principal_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'user'"))
    owner_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_org_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    owner_org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    app_installation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)


class AgentToolCall(UUIDPk, TenantMixin, Timestamps, Base):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "tool_use_id"),
        UniqueConstraint("tenant_id", "idempotency_key"),
        _tenant_fk("run_id", "agent_runs", "fk_agent_tool_calls_run_id_agent_runs", ondelete="CASCADE"),
        _tenant_fk(
            "step_id", "agent_run_steps", "fk_agent_tool_calls_step_id_agent_run_steps", ondelete="CASCADE"
        ),
        _tenant_fk("proposal_id", "agent_proposals", "fk_agent_tool_calls_proposal_id_agent_proposals"),
        CheckConstraint(_in("risk", RISK_CLASSES), name="risk"),
        CheckConstraint(_in("mode", AUTONOMY_MODES), name="mode"),
        CheckConstraint(_in("outcome", TOOL_CALL_OUTCOMES), name="outcome"),
        Index("ix_agent_tool_calls_tool_key_created_at", "tool_key", "created_at"),
        Index(
            "ix_agent_tool_calls_violations", "outcome", postgresql_where=text("outcome = 'policy_violation'")
        ),
        Index("ix_agent_tool_calls_candidate_ids", "candidate_ids", postgresql_using="gin"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_use_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False)
    risk: Mapped[str] = mapped_column(String(10), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    mode_reason: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    args_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    egress: Mapped[bool] = mapped_column(Boolean, nullable=False)
    candidate_ids: Mapped[list[uuid.UUID]] = _uuid_array()
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    executed_as: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class AgentPayload(UUIDPk, TenantMixin, Timestamps, Base):
    """Encrypted payload store (NFR-47): fernet purpose ``agent_payload`` with the tenant key. Never audited."""

    __tablename__ = "agent_payloads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _tenant_fk("run_id", "agent_runs", "fk_agent_payloads_run_id_agent_runs", ondelete="CASCADE"),
        _tenant_fk(
            "step_id", "agent_run_steps", "fk_agent_payloads_step_id_agent_run_steps", ondelete="CASCADE"
        ),
        _tenant_fk(
            "tool_call_id",
            "agent_tool_calls",
            "fk_agent_payloads_tool_call_id_agent_tool_calls",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("kind", PAYLOAD_KINDS), name="kind"),
        Index("ix_agent_payloads_run_id_kind", "run_id", "kind"),
        Index("ix_agent_payloads_expires_at", "expires_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Ordering inside a run (step number the content belongs to; history replay sorts by (step_no, created seq)).
    step_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Epic 66 (500_04): ``owner_principal_id`` is NOT NULL; for user owners it equals ``owner_user_id``. ORM inserts that
# leave it unset (every Phase 4 path) get it copied here, so P1-P4 writers need no change.
def _default_owner_principal(_mapper: Any, _connection: Any, target: Any) -> None:
    if target.owner_principal_id is None and target.owner_user_id is not None:
        target.owner_principal_id = target.owner_user_id
    if target.owner_principal_type is None:
        target.owner_principal_type = "user"


event.listen(AgentRun, "before_insert", _default_owner_principal)
event.listen(AgentProposal, "before_insert", _default_owner_principal)

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "AUTONOMY_MODES",
    "FINAL_RUN_STATUSES",
    "PAYLOAD_KINDS",
    "PROPOSAL_STATUSES",
    "REASON_CODES",
    "RISK_CLASSES",
    "RUN_STATUSES",
    "TOOL_CALL_OUTCOMES",
    "TRIGGER_KINDS",
    "AgentActionPolicy",
    "AgentConfig",
    "AgentPayload",
    "AgentPrincipal",
    "AgentProposal",
    "AgentRun",
    "AgentRunStep",
    "AgentToolCall",
]
