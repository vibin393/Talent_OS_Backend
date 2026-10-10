# ruff: noqa: E501
"""Phase 5 foundation (Epic 66, Story 66.6, spec 0.10): move the legacy secrets into the Secrets Vault.

Per tenant (``app.tenant_id`` set, as 400_04): re-encrypt ``provider_connector_configs.credentials`` /
``webhook_secret`` (owner ``provider_config``) and ``user_connections.access_token_enc`` / ``refresh_token_enc``
(owner ``calendar_connection``) into ``vault_secrets`` under the tenant data key (created when missing), then read
every written row back and compare it byte-exact with the legacy plaintext. Any mismatch raises and the whole
migration rolls back (no partial commit). Values are never logged; only counts are.

Imports only the pure modules ``app.core.kms``, ``app.services.vault.crypto`` (through ``legacy_move``),
``app.core.security`` and settings. Downgrade decrypts the vault rows back into the legacy columns.

Revision ID: 20261009_500_05_p5_vault_move
Revises: 20261009_500_04_p5_alters
Create Date: 2026-10-09
"""

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.kms import get_kms
from app.services.vault import legacy_move

revision: str = "20261009_500_05_p5_vault_move"
down_revision: str | tuple[str, ...] | None = "20261009_500_04_p5_alters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")


def _tenants(bind: sa.engine.Connection) -> list[uuid.UUID]:
    return [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants ORDER BY id")).all()]


def upgrade() -> None:
    bind = op.get_bind()
    kms = get_kms()
    moved = skipped = 0
    for tenant_id in _tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        result = legacy_move.move_tenant(bind, uuid.UUID(str(tenant_id)), kms)
        moved += result["moved"]
        skipped += result["skipped"]
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    log.info(
        "vault move: %d legacy secrets moved and verified; %d undecryptable OAuth tokens skipped",
        moved,
        skipped,
    )


def downgrade() -> None:
    bind = op.get_bind()
    kms = get_kms()
    restored = 0
    for tenant_id in _tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        restored += legacy_move.restore_tenant(bind, uuid.UUID(str(tenant_id)), kms)
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    log.info("vault move downgrade: %d secrets restored to the legacy columns", restored)
