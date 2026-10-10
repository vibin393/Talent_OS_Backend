# ruff: noqa: E501
"""Phase 3 Epic 51 (FR-253) 315_01: DSC-signed invoice / credit-note PDFs.

* ``invoices`` / ``credit_notes`` gain ``signed_pdf_file_id``, ``dsc_status`` (``not_required|pending|signed|failed``)
  and ``dsc_signed_at``. The 216_03 / 216_04 document guards are re-applied with the three DSC columns added to the
  operational allow-list of BOTH the immutability trigger and the period guard (signing may finish after the
  period closes). Amounts, lines and ``content_hash`` never change. Downgrade restores the 216_03 / 216_04
  configuration exactly (the tuples are copied here, never imported from those migration modules).
* ``dsc_policies``: per GST Registration and Client (NULL Client = every Client of that registration).
* ``dsc_sign_requests``: one per ``(kind, doc_id)``; the stored render, the signed file and the verification.

RLS on every new table + internal-only org policy.

Revision ID: 20261007_315_01_dsc
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_period_guard,
    enable_rls,
)

revision: str = "20261007_315_01_dsc"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_fin_carry",)
depends_on: str | Sequence[str] | None = None

# Copied from 20261005_216_03_billing_invoices / 20261005_216_04_billing_notes (identical in both).
OPERATIONAL_216 = (
    "delivery_status",
    "last_sent_at",
    "pdf_file_id",
    "pdf_render_version",
    "einvoice_status",
    "irn",
    "ack_no",
    "ack_at",
    "signed_qr",
    "irn_report_by",
    "irn_cancelled_at",
    "irn_cancel_reason",
)
DSC_COLUMNS = ("signed_pdf_file_id", "dsc_status", "dsc_signed_at")
MUTABLE = ("draft", "pending_approval")
# table -> extra transition columns used by 216_03 / 216_04
DOC_TABLES = {"invoices": ("cancelling_credit_note_id",), "credit_notes": ()}


def _document_guards(table: str, operational: tuple[str, ...], extra_transition: tuple[str, ...]) -> None:
    """Apply the same shape as 216_03 / 216_04 ``_document_guards`` with an explicit operational tuple."""
    enable_immutability(
        table,
        status_column="status",
        mutable_states=MUTABLE,
        allowed_columns=operational,
        allowed_transitions=(("issued", "cancelled"),),
        transition_columns=("cancel_basis", "cancelled_at", *extra_transition),
    )
    cancel_cols = ("status", "cancel_basis", "cancelled_at", *extra_transition)
    enable_period_guard(table, date_column="doc_date", allowed_columns=(*operational, *cancel_cols))


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


def _fk(table: str, column: str, target: str, name: str | None = None) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=name or f"fk_{table}_{column}_{target}",
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- DSC columns + guards
    for table, extra in DOC_TABLES.items():
        op.add_column(table, sa.Column("signed_pdf_file_id", sa.Uuid(), nullable=True))
        op.add_column(
            table,
            sa.Column("dsc_status", sa.String(15), server_default=sa.text("'not_required'"), nullable=False),
        )
        op.add_column(table, sa.Column("dsc_signed_at", sa.DateTime(timezone=True), nullable=True))
        op.create_check_constraint(
            op.f(f"ck_{table}_dsc_status"),
            table,
            "dsc_status IN ('not_required', 'pending', 'signed', 'failed')",
        )
        _document_guards(table, (*OPERATIONAL_216, *DSC_COLUMNS), extra)

    # ---------------------------------------------------------------- dsc_policies
    t = "dsc_policies"
    op.create_table(
        t,
        *_base(t),
        sa.Column("gst_registration_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("provider_config_id", sa.Uuid(), nullable=True),
        sa.Column("signer_ref", sa.String(200), nullable=True),
        sa.Column("reason", sa.String(200), server_default=sa.text("'Invoice issued'"), nullable=False),
        sa.Column("location", sa.String(100), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _fk(t, "gst_registration_id", "gst_registrations"),
        _fk(t, "client_id", "clients"),
        _fk(t, "provider_config_id", "provider_connector_configs", "fk_dsc_policies_provider_config_id"),
    )
    op.create_index("ix_dsc_policies_tenant_id", t, ["tenant_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_dsc_policies_registration_client ON dsc_policies "
        "(tenant_id, gst_registration_id, COALESCE(client_id, '00000000-0000-0000-0000-000000000000'::uuid))"
    )
    enable_rls(t)
    enable_org_rls(t)  # internal sessions only

    # ---------------------------------------------------------------- dsc_sign_requests
    t = "dsc_sign_requests"
    op.create_table(
        t,
        *_base(t),
        sa.Column("kind", sa.String(15), nullable=False),
        sa.Column("doc_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("adapter_key", sa.String(40), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("render_file_id", sa.Uuid(), nullable=True),
        sa.Column("render_sha256", sa.CHAR(64), nullable=True),
        sa.Column("signed_file_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("signer_subject", sa.String(500), nullable=True),
        sa.Column("signer_serial", sa.String(100), nullable=True),
        sa.Column("signing_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verification",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _fk(t, "policy_id", "dsc_policies"),
        _fk(t, "render_file_id", "stored_files"),
        _fk(t, "signed_file_id", "stored_files"),
        _fk(t, "document_id", "documents"),
        sa.UniqueConstraint("tenant_id", "kind", "doc_id", name="uq_dsc_sign_requests_kind_doc"),
        sa.CheckConstraint("kind IN ('invoice', 'credit_note')", name=op.f("ck_dsc_sign_requests_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'signed', 'failed')", name=op.f("ck_dsc_sign_requests_status")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_dsc_sign_requests_attempts")),
    )
    op.create_index("ix_dsc_sign_requests_tenant_status", t, ["tenant_id", "status"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in ("dsc_sign_requests", "dsc_policies"):
        disable_org_rls(t)
        disable_rls(t)
    op.drop_table("dsc_sign_requests")
    op.execute("DROP INDEX IF EXISTS uq_dsc_policies_registration_client")
    op.drop_table("dsc_policies")
    for table, extra in DOC_TABLES.items():
        _document_guards(table, OPERATIONAL_216, extra)  # exactly the 216_03 / 216_04 configuration
        op.drop_constraint(op.f(f"ck_{table}_dsc_status"), table, type_="check")
        op.drop_column(table, "dsc_signed_at")
        op.drop_column(table, "dsc_status")
        op.drop_column(table, "signed_pdf_file_id")
