# ruff: noqa: E501
"""Phase 4 foundation (Epic 53, story 53.10): transcripts, the tenant Recording Policy, the hub tables and the platform
AI controls (spec 0.2 / 0.3).

* ``transcripts`` (audited header) + ``transcript_segments`` (append-only, not audited).
* ``recording_policies`` + the per-tenant seed ``IN`` / NULL / ``candidate_consent`` (coordinator decision 9).
* ``voice_sessions`` (owner 58) and ``interview_captures`` (owner 60): keys, consent and status columns only.
* ``platform_ai_controls`` (platform, non-RLS, one row ``id=1``): global kill switch + provider-outage marker;
  SELECT / UPDATE for ``talent_app``.

Every tenant table: tenant RLS + internal-only org RLS.

Revision ID: 20261008_400_03_p4_hubs
Revises: 20261008_400_02_p4_runtime
Create Date: 2026-10-08
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls
from app.core.settings import get_settings

revision: str = "20261008_400_03_p4_hubs"
down_revision: str | tuple[str, ...] | None = "20261008_400_02_p4_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("transcripts", "transcript_segments", "recording_policies", "voice_sessions", "interview_captures")
SOURCE_KINDS = ("screening_conversation", "voice_session", "interview_capture", "upload")
SPEAKERS = ("agent", "candidate", "interviewer", "recruiter", "unknown")
SESSION_STATUSES = (
    "planned", "consent_pending", "scheduled", "in_progress", "uploaded", "processing", "ready_for_review", "reviewed",
    "failed", "cancelled",
)  # fmt: skip
CAPTURE_STATUSES = ("planned", "consent_pending", "blocked", "uploaded", "processing", "ready", "erased")
SEED_NOTE = "Seeded default (coordinator decision 9): India, recording with the candidate's consent."


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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


def _fk(
    table: str, column: str, target: str, name: str | None = None, ondelete: str = "RESTRICT"
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        name=name or f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
    )


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def upgrade() -> None:
    t = "transcripts"
    op.create_table(
        t,
        *_base(t),
        sa.Column("source_kind", sa.String(24), nullable=False),
        _uuid("source_id"),
        _uuid("candidate_id"),
        _uuid("application_id"),
        _uuid("interview_id"),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("segment_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _uuid("file_id"),
        sa.Column("created_by_kind", sa.String(20), nullable=False),
        _uuid("created_by_id"),
        _uuid("attested_by"),
        _ts("attested_at"),
        _ts("erased_at"),
        _fk(t, "candidate_id", "candidates"),
        _fk(t, "application_id", "applications"),
        _fk(t, "interview_id", "interviews"),
        _fk(t, "file_id", "stored_files"),
        _fk(t, "attested_by", "users"),
        _ck(t, "source_kind", _in("source_kind", SOURCE_KINDS)),
        _ck(t, "status", "status IN ('pending', 'ready', 'failed', 'erased')"),
    )
    op.create_index("ix_transcripts_source", t, ["source_kind", "source_id"])
    op.create_index("ix_transcripts_candidate_id", t, ["candidate_id"])

    t = "transcript_segments"
    op.create_table(
        t,
        *_base(t),
        _uuid("transcript_id", nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("speaker_role", sa.String(12), nullable=False),
        _uuid("speaker_ref"),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("end_ms", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "transcript_id", "seq", name="uq_transcript_segments_tenant_id_transcript_id_seq"
        ),
        _fk(t, "transcript_id", "transcripts", ondelete="CASCADE"),
        _ck(t, "speaker_role", _in("speaker_role", SPEAKERS)),
        _ck(t, "times", "end_ms IS NULL OR start_ms IS NULL OR end_ms >= start_ms"),
        _ck(t, "text_length", "char_length(text) <= 8000"),
    )

    t = "recording_policies"
    op.create_table(
        t,
        *_base(t),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("region", sa.String(10), nullable=True),
        sa.Column("rule", sa.String(20), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        _uuid("updated_by"),
        _fk(t, "updated_by", "users"),
        _ck(t, "rule", "rule IN ('not_permitted', 'candidate_consent', 'all_party_consent')"),
    )
    op.create_index(
        "uq_recording_policies_jurisdiction",
        t,
        ["tenant_id", "country", sa.text("COALESCE(region, '')")],
        unique=True,
    )

    t = "voice_sessions"
    op.create_table(
        t,
        *_base(t),
        _uuid("candidate_id", nullable=False),
        _uuid("application_id"),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("provider_key", sa.String(40), nullable=False),
        _uuid("connector_config_id"),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'planned'"), nullable=False),
        _uuid("voice_consent_id"),
        _uuid("recording_consent_id"),
        sa.Column("jurisdiction", sa.String(20), nullable=True),
        sa.Column("recording_rule", sa.String(20), nullable=True),
        sa.Column("recording_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        _uuid("recording_file_id"),
        _uuid("transcript_id"),
        _uuid("agent_run_id"),
        _uuid("attested_by"),
        _ts("attested_at"),
        _fk(t, "candidate_id", "candidates"),
        _fk(t, "application_id", "applications"),
        _fk(
            t,
            "connector_config_id",
            "provider_connector_configs",
            "fk_voice_sessions_connector_config_id_pcc",
        ),
        _fk(t, "recording_file_id", "stored_files"),
        _fk(t, "transcript_id", "transcripts"),
        _fk(t, "agent_run_id", "agent_runs"),
        _fk(t, "attested_by", "users"),
        _ck(t, "mode", "mode IN ('call', 'upload')"),
        _ck(t, "status", _in("status", SESSION_STATUSES)),
        _ck(t, "recording_allowed", "recording_file_id IS NULL OR recording_allowed"),
    )
    op.create_index("ix_voice_sessions_candidate_id", t, ["candidate_id"])
    op.create_index("ix_voice_sessions_application_id", t, ["application_id"])

    t = "interview_captures"
    op.create_table(
        t,
        *_base(t),
        _uuid("interview_id", nullable=False),
        _uuid("candidate_id", nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'planned'"), nullable=False),
        _uuid("recording_consent_id"),
        sa.Column(
            "participant_acks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("jurisdiction", sa.String(20), nullable=True),
        sa.Column("recording_rule", sa.String(20), nullable=True),
        sa.Column("recording_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        _uuid("file_id"),
        _uuid("transcript_id"),
        _uuid("agent_run_id"),
        sa.UniqueConstraint("tenant_id", "interview_id", name="uq_interview_captures_tenant_id_interview_id"),
        _fk(t, "interview_id", "interviews"),
        _fk(t, "candidate_id", "candidates"),
        _fk(t, "file_id", "stored_files"),
        _fk(t, "transcript_id", "transcripts"),
        _fk(t, "agent_run_id", "agent_runs"),
        _ck(t, "status", _in("status", CAPTURE_STATUSES)),
        _ck(t, "recording_allowed", "file_id IS NULL OR recording_allowed"),
    )

    for table in TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_rls(table)
        enable_org_rls(table)

    # ---------------------------------------------------------------- platform AI controls
    t = "platform_ai_controls"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        _ts("global_kill_switch_at"),
        _uuid("global_kill_switch_by"),
        sa.Column("global_kill_switch_reason", sa.String(500), nullable=True),
        _ts("provider_down_since"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _ck(t, "singleton", "id = 1"),
    )
    op.execute("INSERT INTO platform_ai_controls (id) VALUES (1)")
    role = get_settings().db_app_user
    op.execute(f"REVOKE INSERT, DELETE, TRUNCATE ON {t} FROM {role}")
    op.execute(f"GRANT SELECT, UPDATE ON {t} TO {role}")

    # ---------------------------------------------------------------- Recording Policy seed per existing tenant
    bind = op.get_bind()
    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants")).all():
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        bind.execute(
            sa.text(
                "INSERT INTO recording_policies (id, tenant_id, country, region, rule, note) "
                "SELECT :id, CAST(:tid AS uuid), 'IN', NULL, 'candidate_consent', :note "
                "WHERE NOT EXISTS (SELECT 1 FROM recording_policies WHERE tenant_id = CAST(:tid AS uuid) "
                "AND country = 'IN' AND region IS NULL)"
            ),
            {"id": uuid.uuid4(), "tid": str(tenant_id), "note": SEED_NOTE},
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    op.drop_table("platform_ai_controls")
    for table in reversed(TABLES):
        disable_org_rls(table)
        disable_rls(table)
        op.drop_table(table)
