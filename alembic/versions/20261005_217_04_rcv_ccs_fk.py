"""Epic 30 217_04 (phase-close integration): ``client_collection_settings.tenant_id`` -> ``tenants.id`` ON DELETE
RESTRICT, the FK every tenant-owned table carries (AD-2; ``tests/core/test_rls.py``). 217_03 created the table with
only the composite ``(tenant_id, client_id)`` FK to ``clients``.

Revision ID: 20261005_217_04_rcv_ccs_fk
Revises: 20261005_217_03_rcv_coll_close
Create Date: 2026-10-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261005_217_04_rcv_ccs_fk"
down_revision: str | tuple[str, ...] | None = "20261005_217_03_rcv_coll_close"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "fk_client_collection_settings_tenant_id_tenants"


def upgrade() -> None:
    op.create_foreign_key(_NAME, "client_collection_settings", "tenants", ["tenant_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    op.drop_constraint(_NAME, "client_collection_settings", type_="foreignkey")
