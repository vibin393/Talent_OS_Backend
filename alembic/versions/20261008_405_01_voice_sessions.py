# ruff: noqa: E501
"""Voice Screening (Epic 58, stories 58.1 / 58.3): ALTER the ``voice_sessions`` hub (human id, requester / owner, consent
link, confirmed location, call times, questionnaire, transcript source, recording deletion / withdrawal, AI summary and
gaps, review, failure, the P1 screening row); new ``voice_upload_attestations`` and ``voice_settings``; the system email
template key ``voice_consent_request`` (coordinator decision C-11, approved P1 CHECK widening).

New tables: tenant RLS + internal-only org RLS.

Revision ID: 20261008_405_01_voice_sessions
Revises: 20261008_400_04_p4_alters
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261008_405_01_voice_sessions"
down_revision: str | tuple[str, ...] | None = "20261008_400_04_p4_alters"
branch_labels: str | Sequence[str] | None = ("p4_voice",)
depends_on: str | Sequence[str] | None = None

HUB = "voice_sessions"
TABLES = ("voice_upload_attestations", "voice_settings")
OPEN = "status IN ('planned', 'consent_pending', 'scheduled', 'in_progress', 'uploaded', 'processing')"
OLD_KEYS = (
    "submission", "interview_invite", "interview_reminder", "rejection", "offer", "application_confirmation",
    "ai_screening_invite", "client_feedback_request", "custom",
)  # fmt: skip
NEW_KEYS = (*OLD_KEYS, "voice_consent_request")


def _keys(values: tuple[str, ...]) -> str:
    return f"key IN ({', '.join(repr(v) for v in values)})"


def _fk(column: str, target: str, name: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    t = HUB
    op.add_column(t, sa.Column("human_id", sa.String(20), nullable=True))
    op.add_column(t, sa.Column("requested_by", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("consent_link_id", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("confirmed_country", sa.CHAR(2), nullable=True))
    op.add_column(t, sa.Column("confirmed_region", sa.String(10), nullable=True))
    for col in ("scheduled_at", "started_at", "ended_at"):
        op.add_column(t, sa.Column(col, sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("duration_s", sa.Integer(), nullable=True))
    op.add_column(t, sa.Column("script_ref", sa.String(80), nullable=True))
    op.add_column(t, sa.Column("questionnaire_id", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("questionnaire_version", sa.Integer(), nullable=True))
    op.add_column(t, sa.Column("transcript_source", sa.String(10), nullable=True))
    op.add_column(t, sa.Column("recording_media_type", sa.String(40), nullable=True))
    op.add_column(t, sa.Column("recording_deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        t, sa.Column("gaps", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False)
    )
    op.add_column(t, sa.Column("reviewed_by", sa.Uuid(), nullable=True))
    op.add_column(t, sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(t, sa.Column("failure_code", sa.String(40), nullable=True))
    op.add_column(t, sa.Column("screening_id", sa.Uuid(), nullable=True))
    # The hub is empty in practice; backfill defensively before NOT NULL.
    op.execute(
        "UPDATE voice_sessions v SET human_id = 'VS-L' || substr(replace(v.id::text, '-', ''), 1, 12) WHERE human_id IS NULL"
    )
    op.execute(
        "UPDATE voice_sessions v SET owner_user_id = a.owner_id FROM applications a "
        "WHERE v.owner_user_id IS NULL AND a.id = v.application_id AND a.tenant_id = v.tenant_id"
    )
    op.execute(
        "UPDATE voice_sessions v SET owner_user_id = COALESCE(v.attested_by, (SELECT u.id FROM users u "
        "WHERE u.tenant_id = v.tenant_id ORDER BY u.created_at LIMIT 1)) WHERE v.owner_user_id IS NULL"
    )
    op.alter_column(t, "human_id", existing_type=sa.String(20), nullable=False)
    op.alter_column(t, "owner_user_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_voice_sessions_requested_by_users", t, "users", ["tenant_id", "requested_by"], ["tenant_id", "id"],
        ondelete="RESTRICT",
    )  # fmt: skip
    op.create_foreign_key(
        "fk_voice_sessions_owner_user_id_users", t, "users", ["tenant_id", "owner_user_id"], ["tenant_id", "id"],
        ondelete="RESTRICT",
    )  # fmt: skip
    op.create_check_constraint(
        op.f("ck_voice_sessions_transcript_source"), t,
        "transcript_source IS NULL OR transcript_source IN ('provider', 'upload')",
    )  # fmt: skip
    op.create_unique_constraint("uq_voice_sessions_human_id", t, ["tenant_id", "human_id"])
    op.create_index(
        "uq_voice_sessions_open",
        t,
        ["tenant_id", "application_id"],
        unique=True,
        postgresql_where=sa.text(OPEN),
    )

    t = "voice_upload_attestations"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("voice_session_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("statement_version", sa.String(20), nullable=False),
        sa.Column("consent_obtained", sa.Boolean(), nullable=False),
        sa.Column("policy_followed", sa.Boolean(), nullable=False),
        sa.Column("attested_by", sa.Uuid(), nullable=False),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gate_snapshot", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _fk("voice_session_id", "voice_sessions", "fk_voice_upload_attestations_session_id", "CASCADE"),
        _fk("file_id", "stored_files", "fk_voice_upload_attestations_file_id_stored_files"),
        _fk("attested_by", "users", "fk_voice_upload_attestations_attested_by_users"),
        _ck(t, "kind", "kind IN ('recording', 'transcript')"),
        _ck(t, "attested", "consent_obtained AND policy_followed"),
    )
    op.create_index("ix_voice_upload_attestations_tenant_id", t, ["tenant_id"])
    op.create_index("ix_voice_upload_attestations_session_id", t, ["voice_session_id"])

    t = "voice_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("default_provider", sa.String(10), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("consent_link_days", sa.SmallInteger(), server_default=sa.text("7"), nullable=False),
        sa.Column(
            "consent_channel_order",
            postgresql.JSONB(),
            server_default=sa.text('\'["email", "whatsapp", "sms"]\'::jsonb'),
            nullable=False,
        ),
        sa.Column("max_upload_mb", sa.SmallInteger(), server_default=sa.text("200"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=f"pk_{t}"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{t}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        _ck(t, "default_provider", "default_provider IN ('manual', 'fake')"),
        _ck(t, "consent_link_days", "consent_link_days BETWEEN 1 AND 14"),
        _ck(t, "max_upload_mb", "max_upload_mb BETWEEN 1 AND 200"),
    )
    op.create_index("ix_voice_settings_tenant_id", t, ["tenant_id"])

    for table in TABLES:
        enable_rls(table)
        enable_org_rls(table)

    # C-11: the consent-request system email template key.
    op.drop_constraint(op.f("ck_email_templates_key"), "email_templates", type_="check")
    op.create_check_constraint(op.f("ck_email_templates_key"), "email_templates", _keys(NEW_KEYS))


def downgrade() -> None:
    op.execute("DELETE FROM email_templates WHERE key = 'voice_consent_request'")
    op.drop_constraint(op.f("ck_email_templates_key"), "email_templates", type_="check")
    op.create_check_constraint(op.f("ck_email_templates_key"), "email_templates", _keys(OLD_KEYS))
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
    t = HUB
    op.drop_index("uq_voice_sessions_open", table_name=t)
    op.drop_constraint("uq_voice_sessions_human_id", t, type_="unique")
    op.drop_constraint(op.f("ck_voice_sessions_transcript_source"), t, type_="check")
    op.drop_constraint("fk_voice_sessions_owner_user_id_users", t, type_="foreignkey")
    op.drop_constraint("fk_voice_sessions_requested_by_users", t, type_="foreignkey")
    for column in (
        "screening_id", "failure_code", "reviewed_at", "reviewed_by", "gaps", "summary", "withdrawn_at",
        "recording_deleted_at", "recording_media_type", "transcript_source", "questionnaire_version",
        "questionnaire_id", "script_ref", "duration_s", "ended_at", "started_at", "scheduled_at", "confirmed_region",
        "confirmed_country", "consent_link_id", "owner_user_id", "requested_by", "human_id",
    ):  # fmt: skip
        op.drop_column(t, column)
