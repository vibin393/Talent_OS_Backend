"""Epic 12 privacy: ``candidates.ai_consent_state`` / ``erased_at``, secure links (Epic 6 A5 primitive),
consent notices + consents (Epic 6 A4 minimal model), DSRs, legal holds, erasure runs, retention. Seeds the
Grievance Officer role and the default English consent notices for existing tenants.

Revision ID: 20261001_121
Revises: 20260930_07
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261001_121"
down_revision: str | tuple[str, ...] | None = "20260930_07"
branch_labels: str | Sequence[str] | None = ("privacy",)
depends_on: str | Sequence[str] | None = "20261001_35"


GRIEVANCE_OFFICER = {
    "key": "grievance_officer",
    "name": "Grievance Officer",
    "description": "Handles data subject requests (access, correction, erasure, consent withdrawal).",
}
GRIEVANCE_PERMISSIONS = {
    "dsr:read": "tenant",
    "dsr:process": "tenant",
    "consent:read": "tenant",
    "candidate:read": "tenant",
}
DEFAULT_NOTICES = {
    "data_processing": (
        "Processing of your personal data",
        "We process the personal data in your profile to consider you for job opportunities.",
    ),
    "ai_processing": (
        "AI-assisted processing",
        "We may use AI tools to extract information from your resume, match you to roles and summarise "
        "screening answers. A person always makes the decisions.",
    ),
    "client_submission": (
        "Sharing your profile with a client",
        "With your consent we share your profile with the named client for the named role.",
    ),
    "marketing": (
        "Job alerts and updates",
        "We may contact you about other opportunities that match your profile.",
    ),
}
CANDIDATE_CHECK = "ai_consent_state IN ('granted', 'pending', 'withdrawn', 'not_required')"


def _seed_tenants(bind: sa.engine.Connection) -> None:
    import json
    import uuid

    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants")).all():
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        bind.execute(
            sa.text(
                "INSERT INTO roles (id, tenant_id, key, name, description, is_system, permissions) "
                "VALUES (:id, :tid, :key, :name, :description, true, CAST(:permissions AS jsonb)) "
                "ON CONFLICT (tenant_id, key) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                **GRIEVANCE_OFFICER,
                "permissions": json.dumps(GRIEVANCE_PERMISSIONS),
            },
        )
        for purpose, (title, body) in DEFAULT_NOTICES.items():
            bind.execute(
                sa.text(
                    "INSERT INTO consent_notices (id, tenant_id, purpose, locale, version, title, body_md, active) "
                    "VALUES (:id, :tid, :purpose, 'en', 1, :title, :body, true) ON CONFLICT DO NOTHING"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "purpose": purpose, "title": title, "body": body},
            )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column(
            "ai_consent_state", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
    )
    op.add_column("candidates", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_candidates_ai_consent_state"), "candidates", ["ai_consent_state"], unique=False)
    op.create_check_constraint("ai_consent_state", "candidates", CANDIDATE_CHECK)
    op.add_column("stored_files", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "secure_links",
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column("subject_type", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("use_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_secure_links_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secure_links")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_secure_links_token_hash")),
    )
    op.create_index(op.f("ix_secure_links_subject_id"), "secure_links", ["subject_id"], unique=False)
    op.create_index(op.f("ix_secure_links_tenant_id"), "secure_links", ["tenant_id"], unique=False)
    enable_rls("secure_links")
    op.create_table(
        "secure_link_lookups",
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("link_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_secure_link_lookups")),
    )
    op.create_index(op.f("ix_secure_link_lookups_link_id"), "secure_link_lookups", ["link_id"], unique=False)
    op.create_index(
        op.f("ix_secure_link_lookups_tenant_id"), "secure_link_lookups", ["tenant_id"], unique=False
    )
    op.create_table(
        "consent_notices",
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("locale", sa.String(length=8), server_default=sa.text("'en'"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "locale IN ('en', 'as', 'bn', 'brx', 'doi', 'gu', 'hi', 'kn', 'ks', 'kok', 'mai', 'ml', 'mni', 'mr', 'ne', 'or', 'pa', 'sa', 'sat', 'sd', 'ta', 'te', 'ur')",
            name=op.f("ck_consent_notices_locale"),
        ),
        sa.CheckConstraint(
            "purpose IN ('data_processing', 'ai_processing', 'client_submission', 'marketing')",
            name=op.f("ck_consent_notices_purpose"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_consent_notices_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_notices")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_consent_notices_tenant_id_id")),
        sa.UniqueConstraint(
            "tenant_id",
            "purpose",
            "locale",
            "version",
            name=op.f("uq_consent_notices_tenant_id_purpose_locale_version"),
        ),
    )
    op.create_index(op.f("ix_consent_notices_tenant_id"), "consent_notices", ["tenant_id"], unique=False)
    op.create_index(
        "uq_consent_notices_active",
        "consent_notices",
        ["tenant_id", "purpose", "locale"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    enable_rls("consent_notices")
    op.create_table(
        "consents",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("notice_id", sa.Uuid(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by_kind", sa.String(length=20), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "purpose <> 'client_submission' OR client_id IS NOT NULL",
            name=op.f("ck_consents_client_required"),
        ),
        sa.CheckConstraint(
            "purpose IN ('data_processing', 'ai_processing', 'client_submission', 'marketing')",
            name=op.f("ck_consents_purpose"),
        ),
        sa.CheckConstraint(
            "source IN ('candidate_link', 'candidate_portal', 'application', 'recruiter_attestation', 'import')",
            name=op.f("ck_consents_source"),
        ),
        sa.CheckConstraint("status IN ('granted', 'withdrawn')", name=op.f("ck_consents_status")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_consents_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_consents_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consents")),
    )
    op.create_index("ix_consents_candidate_purpose", "consents", ["candidate_id", "purpose"], unique=False)
    op.create_index(op.f("ix_consents_tenant_id"), "consents", ["tenant_id"], unique=False)
    enable_rls("consents")
    op.create_table(
        "dsr_requests",
        sa.Column("human_id", sa.String(length=20), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("requester_name", sa.String(length=200), nullable=True),
        sa.Column("requester_email", postgresql.CITEXT(), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'received'"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_to", sa.Uuid(), nullable=True),
        sa.Column("identity_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("identity_verified_by", sa.Uuid(), nullable=True),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("due_soon_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("overdue_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "channel IN ('candidate_portal', 'email', 'phone', 'letter', 'other')",
            name=op.f("ck_dsr_requests_channel"),
        ),
        sa.CheckConstraint(
            "kind IN ('access', 'correction', 'erasure', 'consent_withdrawal')",
            name=op.f("ck_dsr_requests_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('received', 'in_progress', 'completed', 'refused')",
            name=op.f("ck_dsr_requests_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_dsr_requests_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_dsr_requests_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dsr_requests")),
        sa.UniqueConstraint("tenant_id", "human_id", name=op.f("uq_dsr_requests_tenant_id_human_id")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_dsr_requests_tenant_id_id")),
    )
    op.create_index("ix_dsr_requests_candidate_id", "dsr_requests", ["candidate_id"], unique=False)
    op.create_index("ix_dsr_requests_status_due", "dsr_requests", ["status", "due_at"], unique=False)
    op.create_index(op.f("ix_dsr_requests_tenant_id"), "dsr_requests", ["tenant_id"], unique=False)
    enable_rls("dsr_requests")
    op.create_table(
        "legal_holds",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("placed_by", sa.Uuid(), nullable=True),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", sa.Uuid(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_legal_holds_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_legal_holds_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legal_holds")),
    )
    op.create_index("ix_legal_holds_candidate_id", "legal_holds", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_legal_holds_tenant_id"), "legal_holds", ["tenant_id"], unique=False)
    enable_rls("legal_holds")
    op.create_table(
        "erasure_runs",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("dsr_id", sa.Uuid(), nullable=True),
        sa.Column("retention_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "handler_results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("reason IN ('dsr', 'retention')", name=op.f("ck_erasure_runs_reason")),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name=op.f("ck_erasure_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_erasure_runs_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_erasure_runs_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_erasure_runs")),
    )
    op.create_index("ix_erasure_runs_candidate_id", "erasure_runs", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_erasure_runs_dsr_id"), "erasure_runs", ["dsr_id"], unique=False)
    op.create_index(
        op.f("ix_erasure_runs_retention_run_id"), "erasure_runs", ["retention_run_id"], unique=False
    )
    op.create_index(op.f("ix_erasure_runs_tenant_id"), "erasure_runs", ["tenant_id"], unique=False)
    enable_rls("erasure_runs")
    op.create_table(
        "retention_policies",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_inactive_months", sa.Integer(), nullable=True),
        sa.Column("auto_execute", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("audit_retention_years", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "audit_retention_years BETWEEN 1 AND 10", name=op.f("ck_retention_policies_audit_years")
        ),
        sa.CheckConstraint(
            "candidate_inactive_months IS NULL OR candidate_inactive_months BETWEEN 6 AND 120",
            name=op.f("ck_retention_policies_inactive_months"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_retention_policies_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_retention_policies")),
    )
    enable_rls("retention_policies")
    op.create_table(
        "retention_runs",
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidate_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("executed_by", sa.Uuid(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_execute_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('previewed', 'executing', 'completed', 'cancelled', 'expired')",
            name=op.f("ck_retention_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_retention_runs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retention_runs")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_retention_runs_tenant_id_id")),
    )
    op.create_index(op.f("ix_retention_runs_tenant_id"), "retention_runs", ["tenant_id"], unique=False)
    enable_rls("retention_runs")
    op.create_table(
        "retention_run_items",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=30), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'erased', 'skipped_now_active', 'failed')",
            name=op.f("ck_retention_run_items_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_retention_run_items_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["retention_runs.tenant_id", "retention_runs.id"],
            name="fk_retention_run_items_run_id_retention_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_retention_run_items_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retention_run_items")),
        sa.UniqueConstraint(
            "run_id", "candidate_id", name=op.f("uq_retention_run_items_run_id_candidate_id")
        ),
    )
    op.create_index(
        op.f("ix_retention_run_items_tenant_id"), "retention_run_items", ["tenant_id"], unique=False
    )
    enable_rls("retention_run_items")
    _seed_tenants(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants")).all():
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        bind.execute(
            sa.text(
                "DELETE FROM roles r WHERE r.key = 'grievance_officer' AND r.is_system "
                "AND NOT EXISTS (SELECT 1 FROM user_roles ur WHERE ur.role_id = r.id)"
            )
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    disable_rls("retention_run_items")
    op.drop_table("retention_run_items")
    disable_rls("retention_runs")
    op.drop_table("retention_runs")
    disable_rls("retention_policies")
    op.drop_table("retention_policies")
    disable_rls("erasure_runs")
    op.drop_table("erasure_runs")
    disable_rls("legal_holds")
    op.drop_table("legal_holds")
    disable_rls("dsr_requests")
    op.drop_table("dsr_requests")
    disable_rls("consents")
    op.drop_table("consents")
    disable_rls("consent_notices")
    op.drop_table("consent_notices")
    op.drop_table("secure_link_lookups")
    disable_rls("secure_links")
    op.drop_table("secure_links")
    op.drop_column("stored_files", "deleted_at")
    op.drop_constraint(op.f("ck_candidates_ai_consent_state"), "candidates", type_="check")
    op.drop_index(op.f("ix_candidates_ai_consent_state"), table_name="candidates")
    op.drop_column("candidates", "erased_at")
    op.drop_column("candidates", "ai_consent_state")
