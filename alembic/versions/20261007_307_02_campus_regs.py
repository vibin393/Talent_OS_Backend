# ruff: noqa: E501
"""Epic 43 stories 43.2 / 43.3 (FR-218): ``registrations`` and ``registration_invites``.

* ``registrations`` -- one per (drive, Candidate); standard answers denormalised into typed columns for the
  ``campus.registration`` semantic source; milestone timestamps feed the funnel. The pipeline link is
  ``application_id`` plus ``applications.source_ref`` (no column on ``applications``).
* ``registration_invites`` -- placement-officer import rows: a personal single-use link was emailed; no Candidate
  or consent exists until the student submits.

Revision ID: 20261007_307_02_campus_regs
Revises: 20261007_307_01_campus_core
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_307_02_campus_regs"
down_revision: str | tuple[str, ...] | None = "20261007_307_01_campus_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "registered",
    "eligible",
    "not_eligible",
    "assessed",
    "shortlisted",
    "interviewed",
    "offered",
    "joined",
    "reneged",
    "rejected",
    "withdrawn",
)


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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"),
    ]


def _fk(name: str, col: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete)


def _ts_col(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    t = "registrations"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("drive_id", sa.Uuid(), nullable=False),
        sa.Column("drive_institution_id", sa.Uuid(), nullable=False),
        sa.Column("institution_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'registered'"), nullable=False),
        sa.Column("answers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("program", sa.String(200), nullable=True),
        sa.Column("branch", sa.String(200), nullable=True),
        sa.Column("cgpa", sa.Numeric(4, 2), nullable=True),
        sa.Column("percentage", sa.Numeric(5, 2), nullable=True),
        sa.Column("backlogs", sa.SmallInteger(), nullable=True),
        sa.Column("graduation_year", sa.SmallInteger(), nullable=True),
        sa.Column("batch_year", sa.SmallInteger(), nullable=False),
        sa.Column("eligibility", postgresql.JSONB(), nullable=True),
        sa.Column("override_by", sa.Uuid(), nullable=True),
        _ts_col("override_at"),
        sa.Column("override_reason", sa.String(1000), nullable=True),
        sa.Column("assessment_invitation_id", sa.Uuid(), nullable=True),
        sa.Column("assessment_flag", sa.String(20), nullable=True),
        sa.Column("score", sa.Numeric(6, 2), nullable=True),
        sa.Column("shortlisted_by", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("offer_id", sa.Uuid(), nullable=True),
        sa.Column("planned_joining_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(10), server_default=sa.text("'link'"), nullable=False),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(1000), nullable=True),
        _ts_col("registered_at", nullable=False),
        *[
            _ts_col(c)
            for c in (
                "eligible_at",
                "assessed_at",
                "shortlisted_at",
                "interviewed_at",
                "offered_at",
                "accepted_at",
                "joined_at",
                "reneged_at",
                "rejected_at",
                "withdrawn_at",
            )
        ],
        *_ts(),
        sa.UniqueConstraint("drive_id", "candidate_id", name="uq_registrations_drive_candidate"),
        sa.CheckConstraint(_in("status", STATUSES), name=op.f("ck_registrations_status")),
        sa.CheckConstraint(_in("source", ("link", "import", "walk_in")), name=op.f("ck_registrations_source")),
        sa.CheckConstraint("cgpa IS NULL OR cgpa BETWEEN 0 AND 10", name=op.f("ck_registrations_cgpa")),
        sa.CheckConstraint("percentage IS NULL OR percentage BETWEEN 0 AND 100", name=op.f("ck_registrations_percentage")),
        sa.CheckConstraint("backlogs IS NULL OR backlogs >= 0", name=op.f("ck_registrations_backlogs")),
        sa.CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100", name=op.f("ck_registrations_score")),
        _fk("fk_registrations_drive_id", "drive_id", "campus_drives"),
        _fk("fk_registrations_drive_institution", "drive_institution_id", "drive_institutions"),
        _fk("fk_registrations_institution_id", "institution_id", "institutions"),
        _fk("fk_registrations_candidate_id", "candidate_id", "candidates"),
        _fk("fk_registrations_override_by", "override_by", "users"),
        _fk("fk_registrations_shortlisted_by", "shortlisted_by", "users"),
        _fk("fk_registrations_invitation", "assessment_invitation_id", "assessment_invitations"),
        _fk("fk_registrations_application_id", "application_id", "applications"),
        _fk("fk_registrations_offer_id", "offer_id", "offers"),
    )
    for name, cols in (
        ("ix_registrations_tenant_id", ["tenant_id"]),
        ("ix_registrations_drive_status", ["drive_id", "status"]),
        ("ix_registrations_drive_institution", ["drive_id", "drive_institution_id"]),
        ("ix_registrations_candidate_id", ["candidate_id"]),
        ("ix_registrations_invitation_id", ["assessment_invitation_id"]),
        ("ix_registrations_application_id", ["application_id"]),
        ("ix_registrations_offer_id", ["offer_id"]),
    ):
        op.create_index(name, t, cols)
    enable_rls(t)
    enable_org_rls(t)

    t = "registration_invites"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("drive_id", sa.Uuid(), nullable=False),
        sa.Column("drive_institution_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("prefill", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column("registration_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_ts(),
        sa.CheckConstraint(_in("status", ("pending", "sent", "registered", "expired")), name=op.f("ck_registration_invites_status")),
        _fk("fk_registration_invites_drive_id", "drive_id", "campus_drives"),
        _fk("fk_registration_invites_di", "drive_institution_id", "drive_institutions"),
        _fk("fk_registration_invites_registration", "registration_id", "registrations"),
        sa.ForeignKeyConstraint(
            ["secure_link_id"], ["secure_links.id"], name="fk_registration_invites_link_id", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_registration_invites_tenant_id", t, ["tenant_id"])
    op.create_index("ix_registration_invites_drive_id", t, ["drive_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    for t in ("registration_invites", "registrations"):
        disable_org_rls(t)
        disable_rls(t)
        op.drop_table(t)
