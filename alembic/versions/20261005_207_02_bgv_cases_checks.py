# ruff: noqa: E501
"""Epic 20 207_02: BGV cases, case timeline, document requests, checks, evidence and discrepancies.

Org RLS: ``bgv_cases`` uses ``worker_col=candidate_id`` (a Worker session sees only its own cases); children
inherit ``candidate_id`` from their case (or check, for evidence) through ``tos_inherit_org``.

Revision ID: 20261005_207_02_bgv_cases_checks
Revises: 20261005_207_01_bgv_config
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import InheritRef, disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_207_02_bgv_cases_checks"
down_revision: str | tuple[str, ...] | None = "20261005_207_01_bgv_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("bgv_cases", "bgv_case_events", "bgv_document_requests", "bgv_checks", "bgv_evidence", "bgv_discrepancies")
UUIDS = postgresql.ARRAY(sa.Uuid())


def _base(table: str, *, timestamps: bool = True) -> list[object]:
    cols: list[object] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]
    if timestamps:
        cols += [
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        ]
    return cols


def _tfk(table: str, column: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=f"fk_{table}_{column}_{target}", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "bgv_cases",
        *_base("bgv_cases"),  # type: ignore[arg-type]
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("package_snapshot", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'initiated'"), nullable=False),
        sa.Column("outcome_qualifier", sa.String(20), nullable=True),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("consent_withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adjudication_reasons", sa.Text(), nullable=True),
        sa.Column("adjudicated_by", sa.Uuid(), nullable=True),
        sa.Column("adjudicated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("evidence_purge_after", sa.Date(), nullable=True),
        sa.Column("evidence_purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("documents_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_doc_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_bgv_cases_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_bgv_cases_tenant_id_human_id"),
        sa.CheckConstraint(
            "status IN ('initiated', 'documents_pending', 'in_verification', 'discrepancy', 'under_review', 'clear', 'failed', 'cancelled')",
            name="ck_bgv_cases_status",
        ),
        sa.CheckConstraint("outcome_qualifier IS NULL OR outcome_qualifier = 'with_exceptions'", name="ck_bgv_cases_qualifier"),
        _tfk("bgv_cases", "candidate_id", "candidates"),
        _tfk("bgv_cases", "placement_id", "placements"),
        _tfk("bgv_cases", "application_id", "applications"),
        _tfk("bgv_cases", "package_id", "bgv_packages"),
    )
    op.create_index("ix_bgv_cases_tenant_id", "bgv_cases", ["tenant_id"])
    op.create_index("ix_bgv_cases_candidate_id", "bgv_cases", ["candidate_id"])
    op.create_index("ix_bgv_cases_placement_id", "bgv_cases", ["placement_id"])
    op.create_index(
        "uq_bgv_cases_placement_package",
        "bgv_cases",
        ["placement_id", "package_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled' AND placement_id IS NOT NULL"),
    )

    op.create_table(
        "bgv_case_events",
        *_base("bgv_case_events", timestamps=False),  # type: ignore[arg-type]
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _tfk("bgv_case_events", "case_id", "bgv_cases", "CASCADE"),
    )
    op.create_index("ix_bgv_case_events_tenant_id", "bgv_case_events", ["tenant_id"])
    op.create_index("ix_bgv_case_events_case_id", "bgv_case_events", ["case_id"])

    op.create_table(
        "bgv_document_requests",
        *_base("bgv_document_requests"),  # type: ignore[arg-type]
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'requested'"), nullable=False),
        sa.Column("file_ids", UUIDS, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("declared", postgresql.JSONB(), nullable=True),
        sa.Column("return_note", sa.Text(), nullable=True),
        sa.Column("redaction_confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_bgv_document_requests_tenant_id_id"),
        sa.UniqueConstraint("case_id", "key", name="uq_bgv_document_requests_case_key"),
        sa.CheckConstraint("kind IN ('upload', 'declaration')", name="ck_bgv_document_requests_kind"),
        sa.CheckConstraint("status IN ('requested', 'submitted', 'accepted', 'returned')", name="ck_bgv_document_requests_status"),
        _tfk("bgv_document_requests", "case_id", "bgv_cases", "CASCADE"),
    )
    op.create_index("ix_bgv_document_requests_tenant_id", "bgv_document_requests", ["tenant_id"])
    op.create_index("ix_bgv_document_requests_case_id", "bgv_document_requests", ["case_id"])

    op.create_table(
        "bgv_checks",
        *_base("bgv_checks"),  # type: ignore[arg-type]
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("seq", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("subject_label", sa.String(300), nullable=False),
        sa.Column("document_request_keys", postgresql.ARRAY(sa.String(60)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("turnaround_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("halted_reason", sa.String(40), nullable=True),
        sa.Column("assignee_kind", sa.String(10), nullable=True),
        sa.Column("assignee_user_id", sa.Uuid(), nullable=True),
        sa.Column("provider_id", sa.Uuid(), nullable=True),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("connector_job_id", sa.Uuid(), nullable=True),
        sa.Column("provider_link_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("start_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("overdue", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("result_note", sa.Text(), nullable=True),
        sa.Column("verifier_name", sa.String(200), nullable=True),
        sa.Column("source_contacted", sa.String(300), nullable=True),
        sa.Column("verified_on", sa.Date(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waived_reason", sa.Text(), nullable=True),
        sa.Column("provider_proposal", postgresql.JSONB(), nullable=True),
        sa.Column("reference_check_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_bgv_checks_tenant_id_id"),
        sa.CheckConstraint(
            "type IN ('identity', 'employment', 'education', 'address', 'reference', 'professional_licence', 'criminal_record', 'drug_test')",
            name="ck_bgv_checks_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'verified', 'discrepancy', 'unable_to_verify', 'waived')", name="ck_bgv_checks_status"
        ),
        sa.CheckConstraint("assignee_kind IS NULL OR assignee_kind IN ('internal', 'provider')", name="ck_bgv_checks_assignee_kind"),
        _tfk("bgv_checks", "case_id", "bgv_cases", "CASCADE"),
        _tfk("bgv_checks", "provider_id", "bgv_providers"),
    )
    op.create_index("ix_bgv_checks_tenant_id", "bgv_checks", ["tenant_id"])
    op.create_index("ix_bgv_checks_case_id", "bgv_checks", ["case_id"])

    op.create_table(
        "bgv_evidence",
        *_base("bgv_evidence", timestamps=False),  # type: ignore[arg-type]
        sa.Column("check_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_kind", sa.String(20), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _tfk("bgv_evidence", "check_id", "bgv_checks", "CASCADE"),
        _tfk("bgv_evidence", "file_id", "stored_files"),
    )
    op.create_index("ix_bgv_evidence_tenant_id", "bgv_evidence", ["tenant_id"])
    op.create_index("ix_bgv_evidence_check_id", "bgv_evidence", ["check_id"])

    op.create_table(
        "bgv_discrepancies",
        *_base("bgv_discrepancies"),  # type: ignore[arg-type]
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("check_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'awaiting_response'"), nullable=False),
        sa.Column("response_due_on", sa.Date(), nullable=False),
        sa.Column("candidate_response", sa.Text(), nullable=True),
        sa.Column("response_file_ids", UUIDS, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raised_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_bgv_discrepancies_tenant_id_id"),
        sa.CheckConstraint("severity IN ('minor', 'major', 'critical')", name="ck_bgv_discrepancies_severity"),
        sa.CheckConstraint(
            "status IN ('awaiting_response', 'responded', 'lapsed', 'resolved')", name="ck_bgv_discrepancies_status"
        ),
        _tfk("bgv_discrepancies", "case_id", "bgv_cases", "CASCADE"),
        _tfk("bgv_discrepancies", "check_id", "bgv_checks", "CASCADE"),
    )
    op.create_index("ix_bgv_discrepancies_tenant_id", "bgv_discrepancies", ["tenant_id"])
    op.create_index("ix_bgv_discrepancies_case_id", "bgv_discrepancies", ["case_id"])

    for table in TABLES:
        enable_rls(table)
    enable_org_rls("bgv_cases", worker_col="candidate_id")
    case_inherit = InheritRef("bgv_cases", "case_id", ("candidate_id",))
    for table in ("bgv_case_events", "bgv_document_requests", "bgv_checks", "bgv_discrepancies"):
        enable_org_rls(table, worker_col="candidate_id", inherit_from=case_inherit)
    # Evidence and the timeline stay internal: provider evidence is never shown in the Worker portal.
    enable_org_rls("bgv_evidence", inherit_from=InheritRef("bgv_checks", "check_id", ("candidate_id",)))


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
