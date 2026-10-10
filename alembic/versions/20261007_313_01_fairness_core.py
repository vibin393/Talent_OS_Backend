# ruff: noqa: E501
"""Epic 49 story 49.1 (FR-246, AD-47, NFR-38): ``fairness_settings``, ``demographic_category_sets``,
``demographic_dimensions`` (dimension keys limited by CHECK to gender / age_band / disability), the default
category set seeded per tenant, ``demographic_self_ids`` (sealed answers) and ``self_id_apply_uses``.

``demographic_self_ids`` carries tenant RLS, the org policy (``worker`` sees only its own row) and the RESTRICTIVE
``fairness_access`` policy: internal sessions read / write nothing unless ``app.fairness_access`` is set to
``aggregate`` (report task), ``erasure`` (erasure / withdrawal) or ``apply_write`` (apply-form submission).

Revision ID: 20261007_313_01_fairness_core
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-07
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_org_rls, disable_rls, enable_org_rls, enable_rls

revision: str = "20261007_313_01_fairness_core"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_fairness",)
depends_on: str | Sequence[str] | None = None

FAIRNESS_ACCESS = (
    "(current_setting('app.org_kind', true) = 'worker' OR "
    "current_setting('app.fairness_access', true) IN ('aggregate', 'erasure', 'apply_write'))"
)

DEFAULT_DIMENSIONS: tuple[tuple[str, str, int, list[tuple[str, str]]], ...] = (
    (
        "gender",
        "Gender",
        1,
        [
            ("woman", "Woman"),
            ("man", "Man"),
            ("non_binary", "Non-binary"),
            ("self_describe_other", "I describe myself another way"),
            ("prefer_not_to_say", "Prefer not to say"),
        ],
    ),
    (
        "age_band",
        "Age band",
        2,
        [
            ("under_25", "Under 25"),
            ("25_34", "25-34"),
            ("35_44", "35-44"),
            ("45_54", "45-54"),
            ("55_plus", "55 or over"),
            ("prefer_not_to_say", "Prefer not to say"),
        ],
    ),
    (
        "disability",
        "Disability",
        3,
        [("yes", "Yes"), ("no", "No"), ("prefer_not_to_say", "Prefer not to say")],
    ),
)


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
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
    ]


def _seed_sql() -> list[str]:
    values = ", ".join(
        "('{k}', '{l}', {p}, '{o}')".format(
            k=key, l=label, p=pos, o=json.dumps([{"key": a, "label": b} for a, b in opts]).replace("'", "''")
        )
        for key, label, pos, opts in DEFAULT_DIMENSIONS
    )
    return [
        "INSERT INTO demographic_category_sets (id, tenant_id, key, name, version, status, is_default) "
        "SELECT gen_random_uuid(), t.id, 'default', 'Default categories', 1, 'active', true FROM tenants t",
        "INSERT INTO demographic_dimensions (id, tenant_id, set_id, key, label, enabled, position, options) "
        "SELECT gen_random_uuid(), s.tenant_id, s.id, v.key, v.label, true, v.pos, v.options::jsonb "
        f"FROM demographic_category_sets s CROSS JOIN (VALUES {values}) AS v(key, label, pos, options)",
        "INSERT INTO fairness_settings (tenant_id, category_set_id) "
        "SELECT s.tenant_id, s.id FROM demographic_category_sets s",
    ]


def upgrade() -> None:
    t = "fairness_settings"
    op.create_table(
        t,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("self_id_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("self_id_on_apply", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("self_id_in_portal", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("category_set_id", sa.Uuid(), nullable=True),
        sa.Column("k", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        sa.Column("ll144_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ll144_notice_text", sa.Text(), nullable=True),
        sa.Column("alternative_process_text", sa.Text(), nullable=True),
        sa.Column("data_retention_text", sa.Text(), nullable=True),
        sa.Column("qualifications_text", sa.Text(), nullable=True),
        sa.Column(
            "notice_lead_business_days", sa.SmallInteger(), server_default=sa.text("10"), nullable=False
        ),
        *_ts(),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_fairness_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_fairness_settings_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("k >= 5", name=op.f("ck_fairness_settings_k")),
        sa.CheckConstraint("notice_lead_business_days >= 10", name=op.f("ck_fairness_settings_notice_days")),
    )

    t = "demographic_category_sets"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'active'"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_demographic_category_sets_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_demographic_category_sets_version"),
        sa.CheckConstraint(
            "status IN ('active', 'retired')", name=op.f("ck_demographic_category_sets_status")
        ),
    )
    op.create_index("ix_demographic_category_sets_tenant_id", t, ["tenant_id"])

    t = "demographic_dimensions"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("set_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(20), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("set_id", "key", name="uq_demographic_dimensions_set_key"),
        # User decision 2026-10-06: no caste, religion or race/ethnicity -- ever.
        sa.CheckConstraint(
            "key IN ('gender', 'age_band', 'disability')", name=op.f("ck_demographic_dimensions_key")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "set_id"],
            ["demographic_category_sets.tenant_id", "demographic_category_sets.id"],
            name="fk_demographic_dimensions_set_id_demographic_category_sets",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_demographic_dimensions_tenant_id", t, ["tenant_id"])
    op.create_index("ix_demographic_dimensions_set_id", t, ["set_id"])

    for stmt in _seed_sql():
        op.execute(stmt)
    for table in ("fairness_settings", "demographic_category_sets", "demographic_dimensions"):
        enable_rls(table)

    t = "demographic_self_ids"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Uuid(), nullable=False),
        sa.Column("set_version", sa.Integer(), nullable=False),
        sa.Column("answers", sa.Text(), nullable=False),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(10), nullable=False),
        *_ts(),
        sa.UniqueConstraint("candidate_id", name="uq_demographic_self_ids_candidate_id"),
        sa.CheckConstraint("source IN ('apply', 'portal')", name=op.f("ck_demographic_self_ids_source")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_demographic_self_ids_candidate_id_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["consent_id"],
            ["consents.id"],
            name="fk_demographic_self_ids_consent_id_consents",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_demographic_self_ids_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t, worker_col="candidate_id")
    op.execute(
        f"CREATE POLICY fairness_access ON {t} AS RESTRICTIVE FOR ALL "
        f"USING {FAIRNESS_ACCESS} WITH CHECK {FAIRNESS_ACCESS}"
    )

    t = "self_id_apply_uses"
    op.create_table(
        t,
        sa.Column("apply_session_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("apply_session_id", name="pk_self_id_apply_uses"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_self_id_apply_uses_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["apply_session_id"],
            ["apply_sessions.id"],
            name="fk_self_id_apply_uses_apply_session_id_apply_sessions",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_self_id_apply_uses_tenant_id", t, ["tenant_id"])
    enable_rls(t)
    enable_org_rls(t)


def downgrade() -> None:
    disable_org_rls("self_id_apply_uses")
    disable_rls("self_id_apply_uses")
    op.drop_table("self_id_apply_uses")
    op.execute("DROP POLICY IF EXISTS fairness_access ON demographic_self_ids")
    disable_org_rls("demographic_self_ids")
    disable_rls("demographic_self_ids")
    op.drop_table("demographic_self_ids")
    for table in ("demographic_dimensions", "demographic_category_sets", "fairness_settings"):
        disable_rls(table)
        op.drop_table(table)
