# ruff: noqa: E501
"""Epic 35 (Privacy platform) 222_01: Phase 2 consent notices, retention classes v2, overrides, DSR subjects.

* Seeds an English v1 notice for each Phase 2 consent purpose per tenant (insert-if-absent; the CHECK widening is
  the foundation's, R12). Seeded notices stay on downgrade (data).
* ALTERs the foundation ``retention_classes`` (R15): ``anchor``, ``expiry_action``, ``is_system``,
  ``floor_months``, ``auto_execute``, ``archived_at``; sets the system defaults (period = ``min_retention_months``)
  and seeds ``timesheets_unbilled`` and ``worker_profile`` (insert-if-absent).
* ``retention_overrides`` (tenant RLS, internal-only org RLS).
* ``dsr_requests.subject_kind/subject_id``, ``erasure_runs.retained_summary``, ``retention_runs.kind``.

Revision ID: 20261005_222_01_privacy_retain
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261005_222_01_privacy_retain"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_privacy_platform",)
depends_on: str | Sequence[str] | None = None

NOTICES: dict[str, tuple[str, str]] = {
    "bgv": (
        "Background verification",
        "With your consent we (or our verification partner) verify the education, employment, identity and other "
        "details you provide, as required for your assignment.",
    ),
    "reference_check": (
        "Reference checks",
        "With your consent we contact the referees you name to ask about your past work.",
    ),
    "vendor_collection": (
        "Profile shared by a supplier",
        "Your profile was shared with us by a staffing supplier who confirmed your consent. We use it to consider "
        "you for the role they submitted you to.",
    ),
    "whatsapp": (
        "WhatsApp messages",
        "With your consent we send you updates about your application or assignment on WhatsApp. You can stop at "
        "any time by replying STOP.",
    ),
    "engagement": (
        "Your engagement with us",
        "While you are on assignment we process your personal, bank and identity details to pay you, invoice our "
        "client and meet legal obligations. This is required while you are on assignment.",
    ),
}

ANCHORS = ("document_date", "fy_end", "adjudicated_at", "signed_at", "placement_end", "period_end")
ACTIONS = ("anonymize", "delete", "report_only")
SUBJECT_KINDS = ("candidate", "contact", "client_user", "vendor_user")

# key -> (label, basis, anchor, months, floor, action)
SYSTEM_CLASSES: dict[str, tuple[str, str, str, int, int, str]] = {
    "financial": ("Financial records", "document_date", "fy_end", 96, 96, "report_only"),
    "signed_agreements": ("Signed agreements", "end_of_engagement", "signed_at", 96, 96, "delete"),
    "timesheets": ("Timesheets", "document_date", "period_end", 96, 96, "report_only"),
    "bgv_evidence": ("Background verification evidence", "adjudication", "adjudicated_at", 12, 12, "delete"),
    "timesheets_unbilled": ("Unbilled timesheets", "document_date", "period_end", 36, 36, "anonymize"),
    "worker_profile": ("Worker profile (bank, identity, onboarding)", "end_of_engagement", "placement_end", 96, 96, "anonymize"),
}


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.add_column("retention_classes", sa.Column("anchor", sa.String(20), server_default=sa.text("'document_date'"), nullable=False))
    op.add_column("retention_classes", sa.Column("expiry_action", sa.String(20), server_default=sa.text("'anonymize'"), nullable=False))
    op.add_column("retention_classes", sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("retention_classes", sa.Column("floor_months", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("retention_classes", sa.Column("auto_execute", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("retention_classes", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("anchor", "retention_classes", _in("anchor", ANCHORS))
    op.create_check_constraint("expiry_action", "retention_classes", _in("expiry_action", ACTIONS))

    bind = op.get_bind()
    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants WHERE status <> 'deleted'")).all():
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        for key, (label, basis, anchor, months, floor, action) in SYSTEM_CLASSES.items():
            bind.execute(
                sa.text(
                    "INSERT INTO retention_classes (id, tenant_id, key, label, basis, min_retention_months, "
                    "delete_after_months, is_default) VALUES (:id, :tid, :key, :label, :basis, :months, NULL, true) "
                    "ON CONFLICT (tenant_id, key) DO NOTHING"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "key": key, "label": label, "basis": basis, "months": months},
            )
            bind.execute(
                sa.text(
                    "UPDATE retention_classes SET anchor = :anchor, expiry_action = :action, is_system = true, "
                    "floor_months = :floor, min_retention_months = GREATEST(min_retention_months, :floor), "
                    "delete_after_months = NULL WHERE tenant_id = :tid AND key = :key"
                ),
                {"tid": tenant_id, "key": key, "anchor": anchor, "action": action, "floor": floor},
            )
        for purpose, (title, body) in NOTICES.items():
            bind.execute(
                sa.text(
                    "INSERT INTO consent_notices (id, tenant_id, purpose, locale, version, title, body_md, active) "
                    "SELECT CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:purpose AS varchar), 'en', 1, CAST(:title AS varchar), CAST(:body AS text), true WHERE NOT EXISTS ("
                    "SELECT 1 FROM consent_notices WHERE tenant_id = CAST(:tid AS uuid) AND purpose = CAST(:purpose AS varchar) AND locale = 'en')"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "purpose": purpose, "title": title, "body": body},
            )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    op.create_check_constraint("floor", "retention_classes", "min_retention_months >= floor_months")

    op.create_table(
        "retention_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("class_key", sa.String(40), nullable=False),
        sa.Column("until_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retention_overrides")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_retention_overrides_tenant_id_tenants"), ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "subject_kind", "subject_id", "class_key", name=op.f("uq_retention_overrides_subject_class")),
        sa.CheckConstraint(_in("subject_kind", SUBJECT_KINDS), name=op.f("ck_retention_overrides_subject_kind")),
    )
    op.create_index(op.f("ix_retention_overrides_tenant_id"), "retention_overrides", ["tenant_id"])
    op.create_index("ix_retention_overrides_class", "retention_overrides", ["tenant_id", "class_key"])
    enable_rls("retention_overrides")
    enable_org_rls("retention_overrides")

    op.add_column("dsr_requests", sa.Column("subject_kind", sa.String(20), server_default=sa.text("'candidate'"), nullable=False))
    op.add_column("dsr_requests", sa.Column("subject_id", sa.Uuid(), nullable=True))
    op.create_check_constraint("subject_kind", "dsr_requests", _in("subject_kind", SUBJECT_KINDS))
    op.execute("UPDATE dsr_requests SET subject_id = candidate_id WHERE subject_id IS NULL AND candidate_id IS NOT NULL")
    op.add_column("erasure_runs", sa.Column("retained_summary", postgresql.JSONB(), nullable=True))
    op.add_column("retention_runs", sa.Column("kind", sa.String(30), server_default=sa.text("'inactive_candidates'"), nullable=False))
    op.create_check_constraint("kind", "retention_runs", _in("kind", ("inactive_candidates", "class_expiry")))


def downgrade() -> None:
    op.drop_constraint(op.f("ck_retention_runs_kind"), "retention_runs", type_="check")
    op.drop_column("retention_runs", "kind")
    op.drop_column("erasure_runs", "retained_summary")
    op.drop_constraint(op.f("ck_dsr_requests_subject_kind"), "dsr_requests", type_="check")
    op.drop_column("dsr_requests", "subject_id")
    op.drop_column("dsr_requests", "subject_kind")
    disable_org_rls("retention_overrides")
    disable_rls("retention_overrides")
    op.drop_index("ix_retention_overrides_class", table_name="retention_overrides")
    op.drop_index(op.f("ix_retention_overrides_tenant_id"), table_name="retention_overrides")
    op.drop_table("retention_overrides")
    bind = op.get_bind()
    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants")).all():
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        bind.execute(
            sa.text("DELETE FROM retention_classes WHERE tenant_id = :tid AND key IN ('timesheets_unbilled', 'worker_profile')"),
            {"tid": tenant_id},
        )
        bind.execute(
            sa.text("UPDATE retention_classes SET min_retention_months = 0, delete_after_months = 12 WHERE tenant_id = :tid AND key = 'bgv_evidence'"),
            {"tid": tenant_id},
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    for name in ("floor", "expiry_action", "anchor"):
        op.drop_constraint(op.f(f"ck_retention_classes_{name}"), "retention_classes", type_="check")
    for col in ("archived_at", "auto_execute", "floor_months", "is_system", "expiry_action", "anchor"):
        op.drop_column("retention_classes", col)
