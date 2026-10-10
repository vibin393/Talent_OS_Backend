"""Epic 65 (story 65.6, FR-323; coordinator decision C-16, approved P3 edit): agent selection points.

Widens ``ck_fairness_runs_selection_point`` with the three Phase 4 selection points ``agent_shortlist_added``,
``agent_screening_flag_advanced`` and ``agent_draft_rating_accepted`` (``selection_point`` is VARCHAR(30)).
The downgrade restores the Phase 3 list and refuses while runs use a new value.

Revision ID: 20261009_412_02_aedt_points
Revises: 20261009_412_01_p4_gov
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261009_412_02_aedt_points"
down_revision: str | tuple[str, ...] | None = "20261009_412_01_p4_gov"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

P3_POINTS = ("stage_transition", "ai_match_threshold", "ai_screening_pass", "assessment_pass")
P4_POINTS = ("agent_shortlist_added", "agent_screening_flag_advanced", "agent_draft_rating_accepted")


def _in(values: tuple[str, ...]) -> str:
    return f"selection_point IN ({', '.join(repr(v) for v in values)})"


def _replace_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_fairness_runs_selection_point"), "fairness_runs", type_="check")
    op.create_check_constraint(op.f("ck_fairness_runs_selection_point"), "fairness_runs", _in(values))


def upgrade() -> None:
    _replace_check(P3_POINTS + P4_POINTS)


def downgrade() -> None:
    used = (
        op.get_bind().execute(sa.text(f"SELECT count(*) FROM fairness_runs WHERE {_in(P4_POINTS)}")).scalar()
    )
    if used:
        msg = "fairness runs use Phase 4 selection points; delete them before downgrading 20261009_412_02"
        raise RuntimeError(msg)
    _replace_check(P3_POINTS)
