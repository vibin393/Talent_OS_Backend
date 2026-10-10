# ruff: noqa: E501
"""Epic 52 (A1, FR-258): seed the Phase 3 default retention classes for every existing tenant.

* ``marketing_logs`` -- campaign message logs (Epic 39 binds ``campaign_messages`` / ``engagement_events``):
  24 months from the document date, then delete.
* ``immigration`` -- immigration case records (Epic 46 binds ``immigration_cases``): 36 months after case close /
  the last permit expiry (the owner's anchor resolver), then anonymise.

Insert-if-absent per tenant (idempotent). New tenants get them from ``retention_classes.seed_p3_classes``
(provisioning + lazy seeding). Downgrade removes only untouched seed rows (``is_system = false``,
``is_default = true``) that no ``retention_overrides`` row references. Data only.

Revision ID: 20261007_316_01_p3_retention
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261007_316_01_p3_retention"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_governance",)
depends_on: str | Sequence[str] | None = None

# key -> (label, basis, anchor, period months, delete_after_months, expiry_action) -- frozen copy of
# ``retention_classes.P3_DEFAULT_CLASSES`` (migrations never import app code).
CLASSES: dict[str, tuple[str, str, str, int, int | None, str]] = {
    "marketing_logs": ("Marketing message logs", "document_date", "document_date", 24, 24, "delete"),
    "immigration": ("Immigration case records", "document_date", "document_date", 36, None, "anonymize"),
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


def _tenants(bind: sa.engine.Connection) -> list[uuid.UUID]:
    return [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants")).all()]


def upgrade() -> None:
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
