# ruff: noqa: E501
"""Epic 40 story 40.1 (FR-202): the assessment item bank.

``assessment_items`` + immutable ``assessment_item_versions`` (server-only ``answer_key`` / ``rubric``; no kind has a
harness / runner / stdin field -- candidate code is never executed), per-tenant ``assessment_settings`` and the
CSV / XLSX ``assessment_item_imports`` batches. Tenant RLS on every table; no org RLS (internal only).

Revision ID: 20261006_304_01_asm_items
Revises: 20261007_300_03_p3_alters
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_304_01_asm_items"
down_revision: str | tuple[str, ...] | None = "20261007_300_03_p3_alters"
branch_labels: str | Sequence[str] | None = ("p3_assessments",)
depends_on: str | Sequence[str] | None = None

ITEM_KINDS = (
    "single", "multi", "true_false", "numeric", "short_text", "code_output", "code_fill", "code_order",
    "code_find_bug", "code_freeform", "essay",
)  # fmt: skip


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _base(table: str, *, timestamps: bool = True) -> list[Any]:
    cols: list[Any] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
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


def _text_array(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
    )


def upgrade() -> None:
    t = "assessment_items"
    op.create_table(
        t,
        *_base(t),
        sa.Column("human_id", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        _text_array("skill_keys"),
        _text_array("tags"),
        sa.Column("difficulty", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("current_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "human_id", name="uq_assessment_items_human_id"),
        sa.CheckConstraint(_in("kind", ITEM_KINDS), name=op.f("ck_assessment_items_kind")),
        sa.CheckConstraint("difficulty BETWEEN 1 AND 5", name=op.f("ck_assessment_items_difficulty")),
    )
    op.create_index("ix_assessment_items_tenant_id", t, ["tenant_id"])
    op.create_index("ix_assessment_items_skill_keys", t, ["skill_keys"], postgresql_using="gin")
    op.create_index("ix_assessment_items_tags", t, ["tags"], postgresql_using="gin")
    enable_rls(t)

    t = "assessment_item_versions"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("language", sa.String(40), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("options", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("points", sa.Numeric(6, 2), nullable=False),
        sa.Column("answer_key", postgresql.JSONB(), nullable=True),
        sa.Column("rubric", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.UniqueConstraint("item_id", "version", name="uq_assessment_item_versions_item_version"),
        _fk("fk_assessment_item_versions_item_id", "item_id", "assessment_items"),
        sa.CheckConstraint("points > 0", name=op.f("ck_assessment_item_versions_points")),
    )
    op.create_index("ix_assessment_item_versions_tenant_id", t, ["tenant_id"])
    op.create_index("ix_assessment_item_versions_item_id", t, ["item_id"])
    enable_rls(t)

    t = "assessment_settings"
    op.create_table(
        t,
        *_base(t),
        sa.Column(
            "allow_auto_pass_non_gating", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "short_answer_confidence_threshold",
            sa.Numeric(4, 3),
            server_default=sa.text("0.800"),
            nullable=False,
        ),
        sa.Column("reminder_hours_before", sa.Integer(), server_default=sa.text("24"), nullable=False),
        sa.Column("default_deadline_days", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("link_grace_days", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("max_signals_per_minute", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("max_signals_per_attempt", sa.Integer(), server_default=sa.text("2000"), nullable=False),
        sa.Column(
            "multi_choice_partial_credit", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", name="uq_assessment_settings_tenant_id"),
        sa.CheckConstraint(
            "short_answer_confidence_threshold BETWEEN 0 AND 1",
            name=op.f("ck_assessment_settings_confidence"),
        ),
    )
    op.create_index("ix_assessment_settings_tenant_id", t, ["tenant_id"])
    enable_rls(t)

    t = "assessment_item_imports"
    op.create_table(
        t,
        *_base(t),
        sa.Column("status", sa.String(20), server_default=sa.text("'previewed'"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("rows", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{t}_tenant_id_id"),
        sa.CheckConstraint(
            _in("status", ("previewed", "committed")), name=op.f("ck_assessment_item_imports_status")
        ),
    )
    op.create_index("ix_assessment_item_imports_tenant_id", t, ["tenant_id"])
    enable_rls(t)


def downgrade() -> None:
    for t in (
        "assessment_item_imports",
        "assessment_settings",
        "assessment_item_versions",
        "assessment_items",
    ):
        disable_rls(t)
        op.drop_table(t)
