# ruff: noqa: E501
"""Epic 40 story 40.2 (FR-202, FR-207): assessment assembly and the provider catalogue.

ALTER hub ``assessments`` (description, instructions, provider config / test, time limit, pass mark, shuffles, attempts
limit, gating default, auto-pass threshold, current version, skill keys, draft sections); immutable
``assessment_versions``; provider cache ``assessment_provider_tests`` and ``assessment_skill_map``.

Revision ID: 20261006_304_02_asm_versions
Revises: 20261006_304_01_asm_items
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_304_02_asm_versions"
down_revision: str | tuple[str, ...] | None = "20261006_304_01_asm_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base(table: str, *, timestamps: bool = True) -> list[Any]:
    cols: list[Any] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="RESTRICT"
        ),
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
    return cols


def _fk(name: str, col: str, target: str, ondelete: str = "RESTRICT") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col], [f"{target}.tenant_id", f"{target}.id"], name=name, ondelete=ondelete
    )


def _assessment_columns() -> list[sa.Column]:
    return [
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("provider_config_id", sa.Uuid(), nullable=True),
        sa.Column("provider_test_ref", sa.String(200), nullable=True),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=True),
        sa.Column("pass_mark_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("shuffle_items", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("shuffle_options", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("attempts_limit", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("gating_default", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_pass_threshold_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "skill_keys", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column(
            "draft_sections", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    ]


CHECKS = (
    ("attempts_limit", "attempts_limit BETWEEN 1 AND 10"),
    ("pass_mark", "pass_mark_pct IS NULL OR pass_mark_pct BETWEEN 0 AND 100"),
    ("auto_pass", "auto_pass_threshold_pct IS NULL OR auto_pass_threshold_pct BETWEEN 0 AND 100"),
    ("time_limit", "time_limit_minutes IS NULL OR time_limit_minutes BETWEEN 1 AND 600"),
)


def upgrade() -> None:
    for col in _assessment_columns():
        op.add_column("assessments", col)
    for name, expr in CHECKS:
        op.create_check_constraint(op.f(f"ck_assessments_{name}"), "assessments", expr)

    t = "assessment_versions"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("total_points", sa.Numeric(8, 2), nullable=False),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=True),
        sa.Column("sections", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.UniqueConstraint("assessment_id", "version", name="uq_assessment_versions_assessment_version"),
        _fk("fk_assessment_versions_assessment_id", "assessment_id", "assessments"),
    )
    op.create_index("ix_assessment_versions_tenant_id", t, ["tenant_id"])
    op.create_index("ix_assessment_versions_assessment_id", t, ["assessment_id"])
    enable_rls(t)

    t = "assessment_provider_tests"
    op.create_table(
        t,
        *_base(t),
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("skills", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("config_id", "external_ref", name="uq_assessment_provider_tests_ref"),
        _fk("fk_assessment_provider_tests_config", "config_id", "provider_connector_configs", "CASCADE"),
    )
    op.create_index("ix_assessment_provider_tests_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "assessment_skill_map"
    op.create_table(
        t,
        *_base(t),
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("provider_skill", sa.String(200), nullable=False),
        sa.Column("skill_key", sa.String(80), nullable=False),
        sa.UniqueConstraint("config_id", "provider_skill", name="uq_assessment_skill_map_skill"),
        _fk("fk_assessment_skill_map_config", "config_id", "provider_connector_configs", "CASCADE"),
    )
    op.create_index("ix_assessment_skill_map_tenant_id", t, ["tenant_id"])
    enable_rls(t)


def downgrade() -> None:
    for t in ("assessment_skill_map", "assessment_provider_tests", "assessment_versions"):
        disable_rls(t)
        op.drop_table(t)
    for name, _ in reversed(CHECKS):
        op.drop_constraint(op.f(f"ck_assessments_{name}"), "assessments", type_="check")
    for col in reversed(_assessment_columns()):
        op.drop_column("assessments", col.name)
