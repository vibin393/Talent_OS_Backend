# ruff: noqa: E501
"""Phase 4 foundation (Epic 53, stories 53.2 / 53.4 / 53.6 / 53.7): shared ALTERs and the retention seed. Contract-layer
head: every Phase 4 epic's first revision uses this revision as its ``down_revision``.

* ``ai_actions``: ``kind`` (single | agent_step), ``agent_key``, ``agent_run_id`` (no FK, log table), ``step_no``,
  ``model_tier``, ``stop_reason``, cache tokens, ``cost_micros``; ix ``(agent_run_id)``, ``(agent_key, created_at)``.
* ``ai_tenant_settings``: enablement-notice acknowledgement, tenant / platform-tenant kill switches, deep-tier opt-in.
* ``audit_logs``: ``agent_run_id``, ``via_proposal_id``, ``agent_key``; ix ``(agent_run_id)``. Grants unchanged.
* Retention classes per existing tenant (insert-if-absent, the P3 pattern): ``agent_payloads`` (6 months, delete),
  ``agent_logs`` (12 months, ``anonymize`` = redact payload-like columns, keep metadata), ``conversation_records``
  (6 months, delete), ``recordings`` (3 months, delete). Frozen copy of ``retention_classes.P4_DEFAULT_CLASSES``.

Revision ID: 20261008_400_04_p4_alters
Revises: 20261008_400_03_p4_hubs
Create Date: 2026-10-08
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261008_400_04_p4_alters"
down_revision: str | tuple[str, ...] | None = "20261008_400_03_p4_hubs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLASSES: dict[str, tuple[str, str, str, int, int | None, str]] = {
    "agent_payloads": ("AI agent payloads", "document_date", "document_date", 6, 6, "delete"),
    "agent_logs": ("AI agent logs", "document_date", "document_date", 12, None, "anonymize"),
    "conversation_records": ("Conversation records", "document_date", "document_date", 6, 6, "delete"),
    "recordings": ("Call and interview recordings", "document_date", "document_date", 3, 3, "delete"),
}

_INSERT = (
    "INSERT INTO retention_classes (id, tenant_id, key, label, basis, min_retention_months, delete_after_months, "
    "is_default, anchor, expiry_action, is_system, floor_months, auto_execute, created_at, updated_at) "
    "SELECT :id, CAST(:tid AS uuid), CAST(:key AS varchar), :label, :basis, :months, :delete_after, true, :anchor, "
    ":action, false, 0, false, now(), now() "
    "WHERE NOT EXISTS (SELECT 1 FROM retention_classes WHERE tenant_id = CAST(:tid AS uuid) "
    "AND key = CAST(:key AS varchar))"
)
_DELETE = (
    "DELETE FROM retention_classes rc WHERE rc.tenant_id = CAST(:tid AS uuid) AND rc.key = CAST(:key AS varchar) "
    "AND rc.is_system = false AND rc.is_default = true AND NOT EXISTS (SELECT 1 FROM retention_overrides o "
    "WHERE o.tenant_id = rc.tenant_id AND o.class_key = rc.key)"
)

AI_ACTION_COLUMNS = (
    ("agent_key", sa.String(40)),
    ("agent_run_id", sa.Uuid()),
    ("step_no", sa.Integer()),
    ("model_tier", sa.String(10)),
    ("stop_reason", sa.String(20)),
    ("cache_read_tokens", sa.Integer()),
    ("cache_write_tokens", sa.Integer()),
    ("cost_micros", sa.BigInteger()),
)
SETTINGS_COLUMNS = (
    ("agentic_enabled_at", sa.DateTime(timezone=True)),
    ("agentic_enabled_by", sa.Uuid()),
    ("agentic_notice_version", sa.String(20)),
    ("kill_switch_at", sa.DateTime(timezone=True)),
    ("kill_switch_by", sa.Uuid()),
    ("kill_switch_reason", sa.String(500)),
    ("platform_kill_switch_at", sa.DateTime(timezone=True)),
    ("platform_kill_switch_by", sa.Uuid()),
    ("platform_kill_switch_reason", sa.String(500)),
)


def _tenants(bind: sa.engine.Connection) -> list[uuid.UUID]:
    return [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants")).all()]


def upgrade() -> None:
    op.add_column(
        "ai_actions", sa.Column("kind", sa.String(20), server_default=sa.text("'single'"), nullable=False)
    )
    for name, type_ in AI_ACTION_COLUMNS:
        op.add_column("ai_actions", sa.Column(name, type_, nullable=True))
    op.create_check_constraint(op.f("ck_ai_actions_kind"), "ai_actions", "kind IN ('single', 'agent_step')")
    op.create_index("ix_ai_actions_agent_run_id", "ai_actions", ["agent_run_id"])
    op.create_index("ix_ai_actions_agent_key_created_at", "ai_actions", ["agent_key", "created_at"])

    for name, type_ in SETTINGS_COLUMNS:
        op.add_column("ai_tenant_settings", sa.Column(name, type_, nullable=True))
    op.add_column(
        "ai_tenant_settings",
        sa.Column("deep_tier_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.add_column("audit_logs", sa.Column("agent_run_id", sa.Uuid(), nullable=True))
    op.add_column("audit_logs", sa.Column("via_proposal_id", sa.Uuid(), nullable=True))
    op.add_column("audit_logs", sa.Column("agent_key", sa.String(40), nullable=True))
    op.create_index("ix_audit_logs_agent_run_id", "audit_logs", ["agent_run_id"])

    bind = op.get_bind()
    for tenant_id in _tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        for key, (label, basis, anchor, months, delete_after, action) in CLASSES.items():
            bind.execute(
                sa.text(_INSERT),
                {
                    "id": uuid.uuid4(), "tid": str(tenant_id), "key": key, "label": label, "basis": basis,
                    "months": months, "delete_after": delete_after, "anchor": anchor, "action": action,
                },
            )  # fmt: skip
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    bind = op.get_bind()
    for tenant_id in _tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        for key in CLASSES:
            bind.execute(sa.text(_DELETE), {"tid": str(tenant_id), "key": key})
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))

    op.drop_index("ix_audit_logs_agent_run_id", table_name="audit_logs")
    op.drop_column("audit_logs", "agent_key")
    op.drop_column("audit_logs", "via_proposal_id")
    op.drop_column("audit_logs", "agent_run_id")

    op.drop_column("ai_tenant_settings", "deep_tier_opt_in")
    for name, _ in reversed(SETTINGS_COLUMNS):
        op.drop_column("ai_tenant_settings", name)

    op.drop_index("ix_ai_actions_agent_key_created_at", table_name="ai_actions")
    op.drop_index("ix_ai_actions_agent_run_id", table_name="ai_actions")
    op.drop_constraint(op.f("ck_ai_actions_kind"), "ai_actions", type_="check")
    for name, _ in reversed(AI_ACTION_COLUMNS):
        op.drop_column("ai_actions", name)
    op.drop_column("ai_actions", "kind")
