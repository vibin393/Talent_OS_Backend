# ruff: noqa: E501
"""Interview Assistant (Epic 60, stories 60.1 / 60.2): ALTER the ``interview_captures`` hub (requester / uploader,
capture kind, confirmed location, blocked reasons, media type, recording deletion, attestation, failure code, detected
speakers); new ``interview_capture_acks`` (interviewers' recording acknowledgements) and ``interview_assistant_settings``.

New tables: tenant RLS + internal-only org RLS.

Revision ID: 20261008_407_01_ia_capture
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_407_01_ia_capture"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_interview_assist",)
depends_on: str | Sequence[str] | None = None

HUB = "interview_captures"
TABLES = ("interview_capture_acks", "interview_assistant_settings")


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    t = HUB
    op.add_column(t, sa.Column("requested_by", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("uploaded_by", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("capture_kind", sa.String(12), nullable=True))
    op.add_column(t, sa.Column("confirmed_country", sa.CHAR(2), nullable=True))
    op.add_column(t, sa.Column("confirmed_region", sa.String(10), nullable=True))
    op.add_column(
        t,
        sa.Column(
            "blocked_reasons", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.add_column(t, sa.Column("file_media_type", sa.String(40), nullable=True))
    op.add_column(t, sa.Column("recording_deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("attested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("failure_code", sa.String(40), nullable=True))
    op.add_column(
        t, sa.Column("speakers", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False)
    )
    op.create_foreign_key(
        "fk_interview_captures_requested_by_users", t, "users", ["tenant_id", "requested_by"], ["tenant_id", "id"],
        ondelete="RESTRICT",
    )  # fmt: skip
    op.create_foreign_key(
        "fk_interview_captures_uploaded_by_users", t, "users", ["tenant_id", "uploaded_by"], ["tenant_id", "id"],
        ondelete="RESTRICT",
    )  # fmt: skip
    op.create_check_constraint(
        op.f("ck_interview_captures_capture_kind"), t,
        "capture_kind IS NULL OR capture_kind IN ('recording', 'transcript', 'both')",
    )  # fmt: skip

    t = "interview_capture_acks"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("participant_kind", sa.String(10), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("interview_id", sa.Uuid(), nullable=True),
        sa.Column("interview_participant_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("statement_version", sa.String(20), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _fk("user_id", "users", "fk_interview_capture_acks_user_id_users"),
        _fk("interview_id", "interviews", "fk_interview_capture_acks_interview_id_interviews", "CASCADE"),
        _fk(
            "interview_participant_id",
            "interview_participants",
            "fk_interview_capture_acks_participant_id",
            "CASCADE",
        ),
        _ck(t, "participant_kind", "participant_kind IN ('user', 'external')"),
        _ck(t, "scope", "scope IN ('standing', 'interview')"),
        _ck(t, "user_kind", "(participant_kind = 'user') = (user_id IS NOT NULL)"),
        _ck(
            t, "standing_scope", "scope <> 'standing' OR (participant_kind = 'user' AND interview_id IS NULL)"
        ),
        _ck(t, "interview_scope", "scope <> 'interview' OR interview_id IS NOT NULL"),
    )
    op.create_index("ix_interview_capture_acks_tenant_id", t, ["tenant_id"])
    op.create_index("ix_interview_capture_acks_interview_id", t, ["interview_id"])
    op.create_index(
        "uq_interview_capture_acks_standing", t, ["tenant_id", "user_id"], unique=True,
        postgresql_where=sa.text("scope = 'standing' AND revoked_at IS NULL"),
    )  # fmt: skip
    op.create_index(
        "uq_interview_capture_acks_participant", t, ["tenant_id", "interview_participant_id"], unique=True,
        postgresql_where=sa.text("scope = 'interview' AND revoked_at IS NULL"),
    )  # fmt: skip

    t = "interview_assistant_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "show_suggestions_by_default", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("allow_recording_upload", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allow_transcript_upload", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("consent_link_days", sa.SmallInteger(), server_default=sa.text("7"), nullable=False),
        sa.Column("max_upload_mb", sa.SmallInteger(), server_default=sa.text("200"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _ck(t, "consent_link_days", "consent_link_days BETWEEN 1 AND 30"),
        _ck(t, "max_upload_mb", "max_upload_mb BETWEEN 1 AND 200"),
    )
    op.create_index("ix_interview_assistant_settings_tenant_id", t, ["tenant_id"])

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    t = HUB
    op.drop_constraint(op.f("ck_interview_captures_capture_kind"), t, type_="check")
    op.drop_constraint("fk_interview_captures_uploaded_by_users", t, type_="foreignkey")
    op.drop_constraint("fk_interview_captures_requested_by_users", t, type_="foreignkey")
    for column in (
        "speakers", "failure_code", "attested_at", "recording_deleted_at", "file_media_type", "blocked_reasons",
        "confirmed_region", "confirmed_country", "capture_kind", "uploaded_at", "uploaded_by", "requested_by",
    ):  # fmt: skip
        op.drop_column(t, column)
