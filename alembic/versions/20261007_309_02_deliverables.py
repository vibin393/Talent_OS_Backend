# ruff: noqa: E501
"""Epic 45 (FR-228, PRD 5.7): ``deliverables`` and the append-only ``deliverable_events``.

Both inherit ``client_id`` (``deliverables`` from ``projects``, events from ``deliverables``) with client org
RLS, so the Client Portal session reads and accepts only its own client's deliverables. ``deliverable_events`` is
immutable (no UPDATE / DELETE); one ``accepted`` event per deliverable (idempotent acceptance).

Revision ID: 20261007_309_02_deliverables
Revises: 20261007_309_01_projects
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import (
    InheritRef,
    disable_immutability,
    disable_org_rls,
    disable_rls,
    enable_immutability,
    enable_org_rls,
    enable_rls,
)

revision: str = "20261007_309_02_deliverables"
down_revision: str | tuple[str, ...] | None = "20261007_309_01_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base(table: str, *, timestamps: bool = True) -> list[Any]:
    cols: list[Any] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
    ]
    if timestamps:
        cols += [
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
        ]
    return [
        *cols,
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete="RESTRICT",
    )


def _ix(table: str, *columns: str) -> None:
    op.create_index(f"ix_{table}_{'_'.join(columns)}", table, list(columns))


def upgrade() -> None:
    t = "deliverables"
    op.create_table(
        t,
        *_base(t),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("milestone_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("acceptance_criteria", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'planned'"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_actor", postgresql.JSONB(), nullable=True),
        sa.Column("last_rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "evidence_file_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _fk(t, "project_id", "projects"),
        _fk(t, "milestone_id", "contract_milestones"),
        sa.CheckConstraint(
            "status IN ('planned', 'in_progress', 'submitted', 'accepted', 'rejected', 'cancelled')",
            name=op.f("ck_deliverables_status"),
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "project_id")
    _ix(t, "milestone_id")
    _ix(t, "client_id")
    enable_rls(t)
    enable_org_rls(
        t, client_col="client_id", inherit_from=InheritRef("projects", "project_id", ("client_id",))
    )

    t = "deliverable_events"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("deliverable_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("actor", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_file_id", sa.Uuid(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _fk(t, "deliverable_id", "deliverables"),
        sa.CheckConstraint(
            "kind IN ('created', 'started', 'submitted', 'accepted', 'rejected', 'cancelled', 'reopened')",
            name=op.f("ck_deliverable_events_kind"),
        ),
    )
    _ix(t, "tenant_id")
    _ix(t, "deliverable_id")
    _ix(t, "client_id")
    op.create_index(
        "uq_deliverable_events_accepted",
        t,
        ["deliverable_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'accepted'"),
    )
    enable_rls(t)
    enable_org_rls(
        t, client_col="client_id", inherit_from=InheritRef("deliverables", "deliverable_id", ("client_id",))
    )
    enable_immutability(t, status_column=None, mutable_states=())


def downgrade() -> None:
    t = "deliverable_events"
    disable_immutability(t)
    disable_org_rls(t)
    disable_rls(t)
    op.drop_table(t)
    t = "deliverables"
    disable_org_rls(t)
    disable_rls(t)
    op.drop_table(t)
