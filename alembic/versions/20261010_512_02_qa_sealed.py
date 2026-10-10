# ruff: noqa: E501
"""Epic 78 (story 78.4, FR-404, AD-90, spec A8): sealed columns for the onboarding Q&A messages.

``onboarding_qa_messages`` gains ``text_sealed`` / ``feedback_note_sealed`` (BYTEA, the Tenant DEK via
``vault_service.seal_field``), ``topic_label`` (non-reversible) and ``question_fingerprint`` (HMAC). ``text`` becomes
nullable, the 2,000-character CHECK moves to the service (enforced before sealing) and a row needs either the plaintext
or the sealed text. The resumable backfill (``knowledge.seal_backfill``) seals existing rows; ``512_03`` seals any
remainder, verifies and drops the plaintext columns.

Downgrade first decrypts every row sealed by the backfill back into ``text`` / ``feedback_note`` (pure
``app.services.vault.qa_seal_move``), then restores NOT NULL and the length CHECK; it refuses if any row still has no
plaintext.

Revision ID: 20261010_512_02_qa_sealed
Revises: 20261010_512_01_copy_drafts
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261010_512_02_qa_sealed"
down_revision: str | tuple[str, ...] | None = "20261010_512_01_copy_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "onboarding_qa_messages"


def upgrade() -> None:
    op.add_column(T, sa.Column("text_sealed", sa.LargeBinary(), nullable=True))
    op.add_column(T, sa.Column("feedback_note_sealed", sa.LargeBinary(), nullable=True))
    op.add_column(T, sa.Column("topic_label", sa.String(60), nullable=True))
    op.add_column(T, sa.Column("question_fingerprint", sa.String(64), nullable=True))
    op.alter_column(T, "text", existing_type=sa.Text(), nullable=True)
    op.drop_constraint(op.f("ck_onboarding_qa_messages_text_length"), T, type_="check")
    op.create_check_constraint(op.f("ck_onboarding_qa_messages_text_present"), T, "text IS NOT NULL OR text_sealed IS NOT NULL")
    op.create_index("ix_onboarding_qa_messages_topic_label", T, ["tenant_id", "topic_label"])
    op.create_index("ix_onboarding_qa_messages_fingerprint", T, ["tenant_id", "question_fingerprint"])


def downgrade() -> None:
    import uuid

    from app.core.kms import get_kms
    from app.services.vault import qa_seal_move

    bind = op.get_bind()
    kms = get_kms()
    for tenant_id in qa_seal_move.tenants(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        qa_seal_move.unseal_tenant(bind, uuid.UUID(str(tenant_id)), kms)
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    missing = bind.execute(sa.text("SELECT count(*) FROM onboarding_qa_messages WHERE text IS NULL")).scalar()
    if missing:
        msg = (
            f"{missing} onboarding Q&A messages have no plaintext and could not be unsealed; "
            "refusing to downgrade 20261010_512_02_qa_sealed"
        )
        raise RuntimeError(msg)
    op.drop_index("ix_onboarding_qa_messages_fingerprint", table_name=T)
    op.drop_index("ix_onboarding_qa_messages_topic_label", table_name=T)
    op.drop_constraint(op.f("ck_onboarding_qa_messages_text_present"), T, type_="check")
    op.create_check_constraint(op.f("ck_onboarding_qa_messages_text_length"), T, "char_length(text) <= 2000")
    op.alter_column(T, "text", existing_type=sa.Text(), nullable=False)
    op.drop_column(T, "question_fingerprint")
    op.drop_column(T, "topic_label")
    op.drop_column(T, "feedback_note_sealed")
    op.drop_column(T, "text_sealed")
