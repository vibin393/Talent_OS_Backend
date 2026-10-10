# ruff: noqa: E501
"""Epic 46 story 46.2 (FR-233, AD-52): ``immigration_counsel``, ``immigration_cases``, append-only ``case_events``
and ``case_dependants``.

Org RLS: ``immigration_cases`` and ``case_events`` worker scope on ``candidate_id`` (the Worker portal shows the
Worker's own Cases; a portal upload writes its own timeline row); counsel and dependants are internal only (policy without org columns). Receipt / passport numbers are
``EncryptedStr`` with ``_last4`` (AD-35).

Revision ID: 20261007_310_02_imm_cases
Revises: 20261007_310_01_visa_types
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261007_310_02_imm_cases"
down_revision: str | tuple[str, ...] | None = "20261007_310_01_visa_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_KINDS = (
    "opened", "stage_changed", "key_date_set", "document_requested", "document_uploaded", "document_verified",
    "document_rejected", "note", "outcome_recorded", "withdrawn", "permit_recorded",
)  # fmt: skip


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _ts() -> list[Any]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(name: str, col: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def upgrade() -> None:
    t = "immigration_counsel"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("firm", sa.String(200), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index("ix_immigration_counsel_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)

    t = "immigration_cases"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("visa_type_id", sa.Uuid(), nullable=False),
        sa.Column("visa_type_version_id", sa.Uuid(), nullable=False),
        sa.Column("country", sa.CHAR(2), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("stage_key", sa.String(60), nullable=True),
        sa.Column("stage_entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("placement_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("business_unit_id", sa.Uuid(), nullable=True),
        sa.Column("counsel_id", sa.Uuid(), nullable=True),
        sa.Column("sponsoring_legal_entity_id", sa.Uuid(), nullable=True),
        sa.Column("key_dates", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("receipt_number", sa.Text(), nullable=True),
        sa.Column("receipt_number_last4", sa.String(4), nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_actor", postgresql.JSONB(), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=True),
        sa.Column("withdraw_reason", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_immigration_cases_human_id"),
        sa.CheckConstraint(
            _in("status", ("open", "in_stage", "approved", "refused", "withdrawn")),
            name=op.f("ck_immigration_cases_status"),
        ),
        sa.CheckConstraint(
            "receipt_number_last4 IS NULL OR length(receipt_number_last4) <= 4",
            name=op.f("ck_immigration_cases_receipt_last4"),
        ),
        _fk("fk_immigration_cases_candidate_id", "candidate_id", "candidates"),
        _fk("fk_immigration_cases_visa_type_id", "visa_type_id", "visa_types"),
        _fk("fk_immigration_cases_version_id", "visa_type_version_id", "visa_type_versions"),
        _fk("fk_immigration_cases_placement_id", "placement_id", "placements"),
        _fk("fk_immigration_cases_project_id", "project_id", "projects"),
        _fk("fk_immigration_cases_demand_id", "demand_id", "demands"),
        _fk("fk_immigration_cases_owner_user_id", "owner_user_id", "users"),
        _fk("fk_immigration_cases_counsel_id", "counsel_id", "immigration_counsel"),
        _fk("fk_immigration_cases_sponsor_le_id", "sponsoring_legal_entity_id", "legal_entities"),
    )
    op.create_index("ix_immigration_cases_tenant_id", t, ["tenant_id"])
    op.create_index("ix_immigration_cases_status_country", t, ["status", "country"])
    op.create_index("ix_immigration_cases_candidate_id", t, ["candidate_id"])
    op.create_index("ix_immigration_cases_owner_user_id", t, ["owner_user_id"])
    op.create_index("ix_immigration_cases_placement_id", t, ["placement_id"])
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")

    t = "case_events"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("from_stage", sa.String(60), nullable=True),
        sa.Column("to_stage", sa.String(60), nullable=True),
        sa.Column("actor", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_in("kind", EVENT_KINDS), name=op.f("ck_case_events_kind")),
        _fk("fk_case_events_case_id", "case_id", "immigration_cases", "CASCADE"),
        _fk("fk_case_events_candidate_id", "candidate_id", "candidates"),
    )
    op.create_index("ix_case_events_tenant_id", t, ["tenant_id"])
    op.create_index("ix_case_events_case_id", t, ["case_id"])
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")
    # Append-only; only ``payload`` / ``actor`` may be blanked (by the erasure handler).
    enable_immutability(t, status_column=None, allowed_columns=("payload", "actor"))

    t = "case_dependants"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("relationship", sa.String(20), nullable=False),
        sa.Column("nationality", sa.CHAR(2), nullable=True),
        sa.Column("passport_number", sa.Text(), nullable=True),
        sa.Column("passport_number_last4", sa.String(4), nullable=True),
        sa.Column("visa_status", sa.String(20), server_default=sa.text("'not_started'"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_ts(),
        sa.CheckConstraint(
            _in("relationship", ("spouse", "child", "parent", "other")),
            name=op.f("ck_case_dependants_relationship"),
        ),
        sa.CheckConstraint(
            _in("visa_status", ("not_started", "in_progress", "approved", "refused", "withdrawn")),
            name=op.f("ck_case_dependants_visa_status"),
        ),
        sa.CheckConstraint(
            "passport_number_last4 IS NULL OR length(passport_number_last4) <= 4",
            name=op.f("ck_case_dependants_last4"),
        ),
        _fk("fk_case_dependants_case_id", "case_id", "immigration_cases", "CASCADE"),
    )
    op.create_index("ix_case_dependants_tenant_id", t, ["tenant_id"])
    op.create_index("ix_case_dependants_case_id", t, ["case_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_immutability("case_events")
    for t in ("case_dependants", "case_events", "immigration_cases", "immigration_counsel"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
