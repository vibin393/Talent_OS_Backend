# ruff: noqa: E501
"""Epic 78 (story 78.4, FR-404, AD-90, spec A8; build plan 7.6): seal the remainder, verify, drop the Q&A plaintext.

1. Per tenant (``app.tenant_id`` set), batches of 500: every row with ``text`` / ``feedback_note`` still in plaintext
   is sealed with the pure ``app.services.vault.crypto`` + ``app.core.kms`` functions (``qa_seal_move``), decrypted back
   and compared, then the plaintext is NULLed. Any mismatch raises and nothing is committed.
2. Verify: no row may keep plaintext (else ``plaintext_remaining``).
3. Drop ``text`` / ``feedback_note`` and the ``text_present`` CHECK; ``text_sealed`` becomes NOT NULL.

Downgrade re-adds the plaintext columns (nullable) and decrypts every row back into them. Values are never logged.
The last revision of the ``p5_p4_hardening`` branch.

Revision ID: 20261010_512_03_qa_plain_drop
Revises: 20261010_512_02_qa_sealed
Create Date: 2026-10-10
"""

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.kms import get_kms
from app.services.vault import qa_seal_move

revision: str = "20261010_512_03_qa_plain_drop"
down_revision: str | tuple[str, ...] | None = "20261010_512_02_qa_sealed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "onboarding_qa_messages"
log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    kms = get_kms()
    sealed = 0
    for tenant_id in qa_seal_move.tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        sealed += qa_seal_move.seal_tenant(bind, uuid.UUID(str(tenant_id)), kms)
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    remaining = bind.execute(
        sa.text("SELECT count(*) FROM onboarding_qa_messages WHERE text IS NOT NULL OR feedback_note IS NOT NULL")
    ).scalar()
    if remaining:
        msg = f"plaintext_remaining: {remaining} onboarding Q&A messages still hold plaintext"
        raise RuntimeError(msg)
    unsealed = bind.execute(sa.text("SELECT count(*) FROM onboarding_qa_messages WHERE text_sealed IS NULL")).scalar()
    if unsealed:
        msg = f"plaintext_remaining: {unsealed} onboarding Q&A messages have no sealed text"
        raise RuntimeError(msg)
    log.info("onboarding Q&A sealing: %d remaining rows sealed and verified in-migration", sealed)
    op.drop_constraint(op.f("ck_onboarding_qa_messages_text_present"), T, type_="check")
    op.drop_column(T, "text")
    op.drop_column(T, "feedback_note")
    op.alter_column(T, "text_sealed", existing_type=sa.LargeBinary(), nullable=False)


def downgrade() -> None:
    op.add_column(T, sa.Column("text", sa.Text(), nullable=True))
    op.add_column(T, sa.Column("feedback_note", sa.String(500), nullable=True))
    op.alter_column(T, "text_sealed", existing_type=sa.LargeBinary(), nullable=True)
    op.create_check_constraint(op.f("ck_onboarding_qa_messages_text_present"), T, "text IS NOT NULL OR text_sealed IS NOT NULL")
    bind = op.get_bind()
    kms = get_kms()
    restored = 0
    for tenant_id in qa_seal_move.tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        restored += qa_seal_move.unseal_tenant(bind, uuid.UUID(str(tenant_id)), kms)
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    log.info("onboarding Q&A sealing downgrade: %d rows restored to plaintext", restored)
