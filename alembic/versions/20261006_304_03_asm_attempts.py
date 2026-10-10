# ruff: noqa: E501
"""Epic 40 stories 40.3-40.7 (FR-203..FR-208): invitations, attempts, proctoring, reviews, Submission shares.

* ALTER hub ``assessment_invitations``: the ``assessment_version_id`` FK (Epic 36 left it plain), accommodations,
  lifecycle, provider, result and decision columns; ``candidate_id`` becomes nullable so erasure can keep an
  anonymous result row (percentile cohorts, A13).
* ``attempts`` (one ``in_progress`` per invitation, AD-44), ``attempt_answers``, append-only ``proctoring_signals``,
  ``proctoring_annotations``, append-only ``review_scores``, ``submission_assessment_shares``.

Tenant RLS on every table. The Candidate Portal reaches attempts only through service queries filtered by the
invitation's ``candidate_id`` (no org RLS needed: internal tables).

Revision ID: 20261006_304_03_asm_attempts
Revises: 20261006_304_02_asm_versions
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import disable_rls, enable_rls

revision: str = "20261006_304_03_asm_attempts"
down_revision: str | tuple[str, ...] | None = "20261006_304_02_asm_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNAL_KINDS = (
    "tab_switch",
    "focus_loss",
    "copy",
    "paste",
    "fullscreen_exit",
    "concurrent_session",
    "ip_change",
)
INV = "assessment_invitations"


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


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


def _invitation_columns() -> list[sa.Column]:
    return [
        sa.Column("accommodation_factor", sa.Numeric(3, 2), server_default=sa.text("1.00"), nullable=False),
        sa.Column("screen_reader_mode", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extended_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("secure_link_id", sa.Uuid(), nullable=True),
        sa.Column("consent_id", sa.Uuid(), nullable=True),
        sa.Column("report_url", sa.String(1000), nullable=True),
        sa.Column("report_file_id", sa.Uuid(), nullable=True),
        sa.Column("points", sa.Numeric(8, 2), nullable=True),
        sa.Column("max_points", sa.Numeric(8, 2), nullable=True),
        sa.Column("section_scores", postgresql.JSONB(), nullable=True),
        sa.Column("skill_scores", postgresql.JSONB(), nullable=True),
        sa.Column("percentile", sa.Numeric(5, 2), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
    ]


def upgrade() -> None:
    # ---------------------------------------------------------------- hub ALTER
    for col in _invitation_columns():
        op.add_column(INV, col)
    op.alter_column(INV, "candidate_id", existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        "fk_assessment_invitations_version_id",
        INV,
        "assessment_versions",
        ["tenant_id", "assessment_version_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_assessment_invitations_accommodation"), INV, "accommodation_factor BETWEEN 1.00 AND 3.00"
    )
    op.create_index(
        "uq_assessment_invitations_idem",
        INV,
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index("ix_assessment_invitations_status_deadline", INV, ["status", "deadline_at"])

    # ---------------------------------------------------------------- attempts
    t = "attempts"
    op.create_table(
        t,
        *_base(t),
        sa.Column("invitation_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'in_progress'"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("section_plan", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column(
            "section_deadlines", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("current_section", sa.String(40), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("last_ip_hash", sa.String(64), nullable=True),
        sa.Column("dropped_signals", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("signal_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_score_total", sa.Numeric(8, 2), nullable=True),
        sa.Column("max_points", sa.Numeric(8, 2), nullable=True),
        sa.UniqueConstraint("invitation_id", "attempt_no", name="uq_attempts_invitation_no"),
        _fk("fk_attempts_invitation_id", "invitation_id", INV),
        sa.CheckConstraint(
            _in("status", ("in_progress", "submitted", "auto_submitted")), name=op.f("ck_attempts_status")
        ),
    )
    op.create_index("ix_attempts_tenant_id", t, ["tenant_id"])
    op.create_index("ix_attempts_status_deadline", t, ["status", "deadline_at"])
    op.create_index(
        "uq_attempts_in_progress",
        t,
        ["invitation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
    )
    enable_rls(t)

    t = "attempt_answers"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("item_version_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("section_key", sa.String(40), nullable=False),
        sa.Column("max_points", sa.Numeric(6, 2), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("auto_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("auto_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("final_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("scored_via", sa.String(10), nullable=True),
        sa.UniqueConstraint("attempt_id", "item_version_id", name="uq_attempt_answers_item"),
        sa.UniqueConstraint("attempt_id", "position", name="uq_attempt_answers_position"),
        _fk("fk_attempt_answers_attempt_id", "attempt_id", "attempts", "CASCADE"),
        _fk("fk_attempt_answers_item_version_id", "item_version_id", "assessment_item_versions"),
        sa.CheckConstraint(
            "scored_via IS NULL OR scored_via IN ('auto', 'review', 'provider')",
            name=op.f("ck_attempt_answers_scored_via"),
        ),
    )
    op.create_index("ix_attempt_answers_tenant_id", t, ["tenant_id"])
    op.create_index(
        "ix_attempt_answers_needs_review",
        t,
        ["attempt_id"],
        postgresql_where=sa.text("needs_review AND final_score IS NULL"),
    )
    enable_rls(t)

    t = "proctoring_signals"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("client_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        _fk("fk_proctoring_signals_attempt_id", "attempt_id", "attempts", "CASCADE"),
        sa.CheckConstraint(_in("kind", SIGNAL_KINDS), name=op.f("ck_proctoring_signals_kind")),
    )
    op.create_index("ix_proctoring_signals_tenant_id", t, ["tenant_id"])
    op.create_index("ix_proctoring_signals_attempt_id", t, ["attempt_id"])
    enable_rls(t)

    t = "proctoring_annotations"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("signal_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _fk("fk_proctoring_annotations_attempt_id", "attempt_id", "attempts", "CASCADE"),
        _fk("fk_proctoring_annotations_signal_id", "signal_id", "proctoring_signals", "CASCADE"),
    )
    op.create_index("ix_proctoring_annotations_tenant_id", t, ["tenant_id"])
    op.create_index("ix_proctoring_annotations_attempt_id", t, ["attempt_id"])
    enable_rls(t)

    t = "review_scores"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Numeric(6, 2), nullable=False),
        sa.Column("rubric_marks", postgresql.JSONB(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("ai_action_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _fk("fk_review_scores_answer_id", "answer_id", "attempt_answers", "CASCADE"),
    )
    op.create_index("ix_review_scores_tenant_id", t, ["tenant_id"])
    op.create_index("ix_review_scores_answer_id", t, ["answer_id"])
    enable_rls(t)

    t = "submission_assessment_shares"
    op.create_table(
        t,
        *_base(t, timestamps=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("invitation_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("submission_id", "invitation_id", name="uq_submission_assessment_shares_pair"),
        _fk("fk_submission_asm_shares_submission", "submission_id", "submissions", "CASCADE"),
        _fk("fk_submission_asm_shares_invitation", "invitation_id", INV, "CASCADE"),
    )
    op.create_index("ix_submission_assessment_shares_tenant_id", t, ["tenant_id"])
    op.create_index("ix_submission_assessment_shares_invitation_id", t, ["invitation_id"])
    enable_rls(t)


def downgrade() -> None:
    for t in (
        "submission_assessment_shares", "review_scores", "proctoring_annotations", "proctoring_signals",
        "attempt_answers", "attempts",
    ):  # fmt: skip
        disable_rls(t)
        op.drop_table(t)
    op.drop_index("ix_assessment_invitations_status_deadline", table_name=INV)
    op.drop_index("uq_assessment_invitations_idem", table_name=INV)
    op.drop_constraint(op.f("ck_assessment_invitations_accommodation"), INV, type_="check")
    op.drop_constraint("fk_assessment_invitations_version_id", INV, type_="foreignkey")
    # Fails loudly if an erased (anonymous) invitation exists -- no data deletion on downgrade.
    op.alter_column(INV, "candidate_id", existing_type=sa.Uuid(), nullable=False)
    for col in reversed(_invitation_columns()):
        op.drop_column(INV, col.name)
