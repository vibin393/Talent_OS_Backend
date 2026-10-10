# ruff: noqa: E501
"""Interview Assistant (Epic 60, story 60.4; coordinator decision C-10, approved P3 edit): interview skill evidence.

* ``skill_suggestions.source`` += ``interview`` (``ck_skill_suggestions_source``);
* ``candidate_skill_evidence.kind`` += ``interview`` (verified class: accepted by an interviewer on that Interview) and
  ``interview_declared`` (declared class: accepted by anyone else); the column widens from VARCHAR(12) to VARCHAR(20)
  so ``interview_declared`` fits.

Downgrade refuses while interview suggestions or evidence exist.

Revision ID: 20261008_407_03_ia_skill_src
Revises: 20261008_407_02_ia_outputs
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261008_407_03_ia_skill_src"
down_revision: str | tuple[str, ...] | None = "20261008_407_02_ia_outputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_SOURCES = "source IN ('ai_resume', 'graph', 'project', 'assessment')"
NEW_SOURCES = "source IN ('ai_resume', 'graph', 'project', 'assessment', 'interview')"
OLD_KINDS = "kind IN ('resume', 'self', 'recruiter', 'assessment', 'project', 'graph')"
NEW_KINDS = "kind IN ('resume', 'self', 'recruiter', 'assessment', 'project', 'graph', 'interview', 'interview_declared')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_skill_suggestions_source"), "skill_suggestions", type_="check")
    op.create_check_constraint(op.f("ck_skill_suggestions_source"), "skill_suggestions", NEW_SOURCES)
    op.drop_constraint(op.f("ck_candidate_skill_evidence_kind"), "candidate_skill_evidence", type_="check")
    op.alter_column(
        "candidate_skill_evidence",
        "kind",
        type_=sa.String(20),
        existing_type=sa.String(12),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_candidate_skill_evidence_kind"), "candidate_skill_evidence", NEW_KINDS
    )


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM skill_suggestions WHERE source = 'interview')"
            " + (SELECT count(*) FROM candidate_skill_evidence WHERE kind IN ('interview', 'interview_declared'))"
        )
    ).scalar()
    if used:
        msg = "interview skill suggestions / evidence exist; erase them before downgrading 20261008_407_03"
        raise RuntimeError(msg)
    op.drop_constraint(op.f("ck_candidate_skill_evidence_kind"), "candidate_skill_evidence", type_="check")
    op.alter_column(
        "candidate_skill_evidence",
        "kind",
        type_=sa.String(12),
        existing_type=sa.String(20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_candidate_skill_evidence_kind"), "candidate_skill_evidence", OLD_KINDS
    )
    op.drop_constraint(op.f("ck_skill_suggestions_source"), "skill_suggestions", type_="check")
    op.create_check_constraint(op.f("ck_skill_suggestions_source"), "skill_suggestions", OLD_SOURCES)
