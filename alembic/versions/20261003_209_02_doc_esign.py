# ruff: noqa: E501
"""Epic 22 (P2-9.3 / P2-9.5): signature requests, signers, signatures, signature events, signer OTPs; the P1
offer e-acceptance columns (``offers.signature_request_id``, ``offer_settings.acceptance_requires_otp``).

Guards (AD-21):
* ``signature_requests``: ``document_version_id`` / ``doc_sha256`` are frozen once the request left ``draft``.
* ``signers``: name / email / order are frozen while the request is sent, partially signed or completed.
* ``signatures``: immutable once the request is completed (child of ``signature_requests``).
* ``signature_events``: append-only (only ``ip`` / ``user_agent`` may be nulled by erasure).
Org RLS: grants on subject ``signature_request`` (per signer organisation, including the Worker).

Revision ID: 20261003_209_02_doc_esign
Revises: 20261003_209_01_doc_templates
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    GrantsRef,
    ParentRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261003_209_02_doc_esign"
down_revision: str | tuple[str, ...] | None = "20261003_209_01_doc_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("signature_requests", "signers", "signatures", "signature_events", "signer_otps")

FROZEN_GUARD = r"""
CREATE OR REPLACE FUNCTION tos_esign_frozen() RETURNS trigger LANGUAGE plpgsql
SET app.org_kind = 'internal' AS $fn$
DECLARE
    p_status text;
BEGIN
    IF TG_TABLE_NAME = 'signature_requests' THEN
        IF OLD.status <> 'draft' AND (NEW.document_version_id IS DISTINCT FROM OLD.document_version_id
                                      OR NEW.doc_sha256 IS DISTINCT FROM OLD.doc_sha256
                                      OR NEW.document_id IS DISTINCT FROM OLD.document_id) THEN
            RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:signature_requests';
        END IF;
        RETURN NEW;
    END IF;
    SELECT status INTO p_status FROM signature_requests WHERE id = OLD.request_id AND tenant_id = OLD.tenant_id;
    IF p_status IN ('sent', 'partially_signed', 'completed') THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:signers';
        END IF;
        IF NEW.name IS DISTINCT FROM OLD.name OR NEW.email IS DISTINCT FROM OLD.email
           OR NEW."order" IS DISTINCT FROM OLD."order" OR NEW.request_id IS DISTINCT FROM OLD.request_id THEN
            RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:signers';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$fn$
"""


def _base() -> list[sa.Column]:  # type: ignore[type-arg]
    return [sa.Column("id", sa.Uuid(), nullable=False), sa.Column("tenant_id", sa.Uuid(), nullable=False)]


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str, *, uq: bool = True) -> list[sa.Constraint]:
    out: list[sa.Constraint] = [
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]
    if uq:
        out.append(sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"))
    return out


def _fk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}",
        ondelete=ondelete,
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.create_table(
        "signature_requests",
        *_base(),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("doc_sha256", sa.String(64), nullable=True),
        sa.Column("subject_kind", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(40), server_default=sa.text("'builtin'"), nullable=False),
        sa.Column("provider_config_id", sa.Uuid(), nullable=True),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("require_otp", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reminder_every_days", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_version_id", sa.Uuid(), nullable=True),
        sa.Column("final_sha256", sa.String(64), nullable=True),
        sa.Column("certificate_id", sa.Uuid(), nullable=False),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        *_ts(),
        *_tenant("signature_requests"),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'partially_signed', 'completed', 'declined', 'expired', 'cancelled')",
            name="ck_signature_requests_status",
        ),
        sa.CheckConstraint(
            "reminder_every_days BETWEEN 1 AND 30", name="ck_signature_requests_reminder_days"
        ),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_signature_requests_tenant_id_human_id"),
        sa.UniqueConstraint("certificate_id", name="uq_signature_requests_certificate_id"),
        _fk("signature_requests", "document_id", "documents"),
        _fk("signature_requests", "document_version_id", "document_versions"),
        _fk("signature_requests", "final_version_id", "document_versions"),
        _fk("signature_requests", "provider_config_id", "provider_connector_configs"),
    )
    op.create_index("ix_signature_requests_tenant_id", "signature_requests", ["tenant_id"])
    op.create_index("ix_signature_requests_subject", "signature_requests", ["subject_kind", "subject_id"])
    op.create_index("ix_signature_requests_status", "signature_requests", ["status", "expires_at"])
    op.create_index("ix_signature_requests_final_sha256", "signature_requests", ["final_sha256"])
    op.create_index("ix_signature_requests_doc_sha256", "signature_requests", ["doc_sha256"])

    op.create_table(
        "signers",
        *_base(),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("otp_verified_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        *_tenant("signers"),
        sa.CheckConstraint(
            "kind IN ('candidate', 'user', 'client_user', 'contact', 'vendor_user')", name="ck_signers_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'notified', 'signed', 'declined', 'void')", name="ck_signers_status"
        ),
        sa.UniqueConstraint("request_id", "order", name="uq_signers_request_id_order"),
        _fk("signers", "request_id", "signature_requests"),
    )
    op.create_index("ix_signers_tenant_id", "signers", ["tenant_id"])
    op.create_index("ix_signers_request_id", "signers", ["request_id"])
    op.create_index("ix_signers_principal", "signers", ["kind", "principal_id"])

    op.create_table(
        "signatures",
        *_base(),
        sa.Column("signer_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("typed_name", sa.String(300), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("otp_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("doc_sha256", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("provider", sa.String(40), server_default=sa.text("'builtin'"), nullable=False),
        *_tenant("signatures"),
        sa.CheckConstraint("channel IN ('portal', 'session', 'secure_link')", name="ck_signatures_channel"),
        sa.UniqueConstraint("signer_id", name="uq_signatures_signer_id"),
        _fk("signatures", "signer_id", "signers"),
        _fk("signatures", "request_id", "signature_requests"),
    )
    op.create_index("ix_signatures_tenant_id", "signatures", ["tenant_id"])
    op.create_index("ix_signatures_request_id", "signatures", ["request_id"])

    op.create_table(
        "signature_events",
        *_base(),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("signer_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        *_tenant("signature_events", uq=False),
        sa.CheckConstraint(
            "kind IN ('created', 'sent', 'viewed', 'otp_sent', 'otp_failed', 'consented', 'signed', 'declined', "
            "'reminded', 'expired', 'cancelled', 'completed', 'verified')",
            name="ck_signature_events_kind",
        ),
        _fk("signature_events", "request_id", "signature_requests"),
    )
    op.create_index("ix_signature_events_tenant_id", "signature_events", ["tenant_id"])
    op.create_index("ix_signature_events_request", "signature_events", ["request_id", "at"])

    op.create_table(
        "signer_otps",
        *_base(),
        sa.Column("signer_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        *_tenant("signer_otps", uq=False),
        _fk("signer_otps", "signer_id", "signers", "CASCADE"),
    )
    op.create_index("ix_signer_otps_tenant_id", "signer_otps", ["tenant_id"])
    op.create_index("ix_signer_otps_signer", "signer_otps", ["signer_id", "created_at"])

    # Platform lookup (no PII): certificate id -> tenant, for the public verify page.
    op.create_table(
        "esign_certificate_lookups",
        sa.Column("certificate_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("certificate_id", name="pk_esign_certificate_lookups"),
    )
    op.create_index("ix_esign_certificate_lookups_tenant_id", "esign_certificate_lookups", ["tenant_id"])

    # P1 offers: e-acceptance on the signature tables (FR-119 last bullet).
    op.add_column("offers", sa.Column("signature_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_offers_signature_request_id",
        "offers",
        "signature_requests",
        ["tenant_id", "signature_request_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "offer_settings",
        sa.Column("acceptance_requires_otp", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    for table in TABLES:
        enable_rls(table)
    enable_org_rls("signature_requests", grants=GrantsRef("signature_request"))
    for table in ("signers", "signatures", "signature_events", "signer_otps"):
        enable_org_rls(table, grants=GrantsRef("signature_request", "request_id"))

    op.execute(FROZEN_GUARD)
    for table in ("signature_requests", "signers"):
        op.execute(f"DROP TRIGGER IF EXISTS tos_esign_frozen ON {table}")
        op.execute(
            f"CREATE TRIGGER tos_esign_frozen BEFORE UPDATE{' OR DELETE' if table == 'signers' else ''} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION tos_esign_frozen()"
        )
    enable_immutability(
        "signatures",
        status_column=None,
        parent=ParentRef(
            "signature_requests",
            "request_id",
            "status",
            ("draft", "sent", "partially_signed", "declined", "expired", "cancelled"),
        ),
    )
    enable_immutability("signature_events", status_column=None, allowed_columns=("ip", "user_agent"))


def downgrade() -> None:
    disable_immutability("signature_events")
    disable_immutability("signatures")
    for table in ("signature_requests", "signers"):
        op.execute(f"DROP TRIGGER IF EXISTS tos_esign_frozen ON {table}")
    op.execute("DROP FUNCTION IF EXISTS tos_esign_frozen()")
    op.drop_table("esign_certificate_lookups")
    op.drop_column("offer_settings", "acceptance_requires_otp")
    op.drop_constraint("fk_offers_signature_request_id", "offers", type_="foreignkey")
    op.drop_column("offers", "signature_request_id")
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
