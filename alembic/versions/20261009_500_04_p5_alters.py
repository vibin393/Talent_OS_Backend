# ruff: noqa: E501
"""Phase 5 foundation (Epic 66): shared ALTERs and the retention seed (spec 0.3).

* ``tenants`` (platform): Sandbox mode columns and ``rate_tier``.
* ``provider_connector_configs``: ``installation_id`` (composite FK -> ``app_installations``) and ``slot_key``; the active
  uniqueness index gains ``slot_key`` (several active job boards per entity). Downgrade refuses while two active configs
  share (tenant, category, entity).
* ``connector_jobs.installation_id``; ``connector_inbound_events.external_event_id`` (+ partial UNIQUE for replay dedup).
* ``agent_runs`` / ``agent_proposals``: nullable ``owner_user_id``, the owner-principal columns (backfilled from
  ``owner_user_id``), ``app_installation_id``; ``agent_runs.external_client``.
* ``audit_logs``: ``app_installation_id``, ``on_behalf_of_user_id`` (no equivalent existed).
* Retention classes per existing tenant (insert-if-absent): ``public_events`` (1 month), ``api_request_logs`` (1 month),
  ``vault_access_logs`` (12 months), ``connector_payloads`` (1 month). Frozen copy of ``P5_DEFAULT_CLASSES``.

Revision ID: 20261009_500_04_p5_alters
Revises: 20261009_500_03_p5_platform
Create Date: 2026-10-09
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261009_500_04_p5_alters"
down_revision: str | tuple[str, ...] | None = "20261009_500_03_p5_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLASSES: dict[str, tuple[str, str, str, int, int | None, str]] = {
    "public_events": ("Public events (outbox)", "document_date", "document_date", 1, 1, "delete"),
    "api_request_logs": ("Public API request logs", "document_date", "document_date", 1, 1, "delete"),
    "vault_access_logs": ("Secret access logs", "document_date", "document_date", 12, 12, "delete"),
    "connector_payloads": ("Connector inbound payloads", "document_date", "document_date", 1, 1, "delete"),
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
OWNER_TABLES = ("agent_runs", "agent_proposals")


def _tenants(bind: sa.engine.Connection) -> list[uuid.UUID]:
    return [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants")).all()]


def upgrade() -> None:
    # ---------------------------------------------------------------- tenants (platform)
    op.add_column(
        "tenants", sa.Column("mode", sa.String(10), server_default=sa.text("'production'"), nullable=False)
    )
    op.add_column("tenants", sa.Column("sandbox_owner_kind", sa.String(12), nullable=True))
    op.add_column("tenants", sa.Column("sandbox_owner_id", sa.Uuid(), nullable=True))
    op.add_column("tenants", sa.Column("sandbox_last_active_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenants", sa.Column("rate_tier", sa.String(20), server_default=sa.text("'standard'"), nullable=False)
    )
    op.create_check_constraint(op.f("ck_tenants_mode"), "tenants", "mode IN ('production', 'sandbox')")
    op.create_check_constraint(
        op.f("ck_tenants_sandbox_owner_kind"),
        "tenants",
        "sandbox_owner_kind IS NULL OR sandbox_owner_kind IN ('partner_org', 'tenant', 'platform')",
    )
    op.create_check_constraint(
        op.f("ck_tenants_sandbox_owner"), "tenants", "(mode = 'sandbox') = (sandbox_owner_kind IS NOT NULL)"
    )

    # ---------------------------------------------------------------- connectors
    t = "provider_connector_configs"
    op.add_column(t, sa.Column("installation_id", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("slot_key", sa.String(40), server_default=sa.text("''"), nullable=False))
    op.create_foreign_key(
        "fk_provider_connector_configs_installation_id_inst",
        t,
        "app_installations",
        ["tenant_id", "installation_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.drop_index("uq_provider_connector_configs_active", table_name=t)
    op.create_index(
        "uq_provider_connector_configs_active",
        t,
        ["tenant_id", "category", "legal_entity_id", "slot_key"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.add_column("connector_jobs", sa.Column("installation_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_connector_jobs_installation_id_status", "connector_jobs", ["installation_id", "status"]
    )
    op.add_column("connector_inbound_events", sa.Column("external_event_id", sa.String(200), nullable=True))
    op.create_index(
        "uq_connector_inbound_events_external_event_id",
        "connector_inbound_events",
        ["tenant_id", "config_id", "external_event_id"],
        unique=True,
        postgresql_where=sa.text("external_event_id IS NOT NULL"),
    )

    # ---------------------------------------------------------------- agent runtime owners (AD-84 / AD-89)
    for t in OWNER_TABLES:
        op.alter_column(t, "owner_user_id", existing_type=sa.Uuid(), nullable=True)
        op.add_column(
            t,
            sa.Column(
                "owner_principal_type", sa.String(20), server_default=sa.text("'user'"), nullable=False
            ),
        )
        op.add_column(t, sa.Column("owner_principal_id", sa.Uuid(), nullable=True))
        op.add_column(t, sa.Column("owner_org_kind", sa.String(10), nullable=True))
        op.add_column(t, sa.Column("owner_org_id", sa.Uuid(), nullable=True))
        op.add_column(t, sa.Column("app_installation_id", sa.Uuid(), nullable=True))
        op.execute(f"UPDATE {t} SET owner_principal_id = owner_user_id WHERE owner_principal_id IS NULL")
        op.alter_column(t, "owner_principal_id", existing_type=sa.Uuid(), nullable=False)
        op.create_check_constraint(
            op.f(f"ck_{t}_owner_principal_type"),
            t,
            "owner_principal_type IN ('user', 'client_user', 'vendor_user', 'candidate')",
        )
        op.create_check_constraint(
            op.f(f"ck_{t}_owner_org_kind"),
            t,
            "owner_org_kind IS NULL OR owner_org_kind IN ('client', 'vendor', 'worker')",
        )
        op.create_check_constraint(
            op.f(f"ck_{t}_owner_user"), t, "(owner_principal_type = 'user') = (owner_user_id IS NOT NULL)"
        )
        op.create_check_constraint(
            op.f(f"ck_{t}_owner_org"), t, "owner_principal_type = 'user' OR owner_org_kind IS NOT NULL"
        )
    op.add_column("agent_runs", sa.Column("external_client", sa.String(120), nullable=True))
    op.create_index(
        "ix_agent_runs_owner_principal",
        "agent_runs",
        ["owner_principal_type", "owner_principal_id", "created_at"],
    )

    # ---------------------------------------------------------------- audit (FR-331)
    op.add_column("audit_logs", sa.Column("app_installation_id", sa.Uuid(), nullable=True))
    op.add_column("audit_logs", sa.Column("on_behalf_of_user_id", sa.Uuid(), nullable=True))
    op.create_index("ix_audit_logs_app_installation_id", "audit_logs", ["app_installation_id"])

    # ---------------------------------------------------------------- retention seed
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
    dupes = bind.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM provider_connector_configs WHERE status = 'active' "
            "GROUP BY tenant_id, category, legal_entity_id HAVING count(*) > 1) d"
        )
    ).scalar_one()
    if dupes:
        msg = "cannot downgrade 500_04: several active connector configs share a (tenant, category, legal entity)"
        raise RuntimeError(msg)
    portal_owned = bind.execute(
        sa.text("SELECT (SELECT count(*) FROM agent_runs WHERE owner_user_id IS NULL) + "
                "(SELECT count(*) FROM agent_proposals WHERE owner_user_id IS NULL)")
    ).scalar_one()  # fmt: skip
    if portal_owned:
        msg = "cannot downgrade 500_04: portal-owned agent runs / proposals exist"
        raise RuntimeError(msg)
    for tenant_id in _tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        for key in CLASSES:
            bind.execute(sa.text(_DELETE), {"tid": str(tenant_id), "key": key})
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))

    op.drop_index("ix_audit_logs_app_installation_id", table_name="audit_logs")
    op.drop_column("audit_logs", "on_behalf_of_user_id")
    op.drop_column("audit_logs", "app_installation_id")

    op.drop_index("ix_agent_runs_owner_principal", table_name="agent_runs")
    op.drop_column("agent_runs", "external_client")
    for t in reversed(OWNER_TABLES):
        for name in ("owner_org", "owner_user", "owner_org_kind", "owner_principal_type"):
            op.drop_constraint(op.f(f"ck_{t}_{name}"), t, type_="check")
        for col in (
            "app_installation_id",
            "owner_org_id",
            "owner_org_kind",
            "owner_principal_id",
            "owner_principal_type",
        ):
            op.drop_column(t, col)
        op.alter_column(t, "owner_user_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_index("uq_connector_inbound_events_external_event_id", table_name="connector_inbound_events")
    op.drop_column("connector_inbound_events", "external_event_id")
    op.drop_index("ix_connector_jobs_installation_id_status", table_name="connector_jobs")
    op.drop_column("connector_jobs", "installation_id")
    t = "provider_connector_configs"
    op.drop_index("uq_provider_connector_configs_active", table_name=t)
    op.create_index(
        "uq_provider_connector_configs_active",
        t,
        ["tenant_id", "category", "legal_entity_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_constraint("fk_provider_connector_configs_installation_id_inst", t, type_="foreignkey")
    op.drop_column(t, "slot_key")
    op.drop_column(t, "installation_id")

    for name in ("sandbox_owner", "sandbox_owner_kind", "mode"):
        op.drop_constraint(op.f(f"ck_tenants_{name}"), "tenants", type_="check")
    for col in ("rate_tier", "sandbox_last_active_at", "sandbox_owner_id", "sandbox_owner_kind", "mode"):
        op.drop_column("tenants", col)
