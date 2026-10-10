"""Epic 34 (Dashboards & Reporting) 221_02: widen ``exports.kind`` to the Phase 2 list kinds (A4).

``exports`` is reporting's own (Epic 11) table; no other epic's table is touched.

Revision ID: 20261005_221_02_rpt_export_kinds
Revises: 20261005_221_01_rpt_summaries
Create Date: 2026-10-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261005_221_02_rpt_export_kinds"
down_revision: str | tuple[str, ...] | None = "20261005_221_01_rpt_summaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

P1 = ("candidates", "demands", "applications", "submissions", "interviews", "offers")
P2 = (
    "leads",
    "opportunities",
    "vendors",
    "vendor_submissions",
    "placements",
    "timesheets",
    "expense_claims",
    "invoices",
    "payments",
    "bgv_cases",
)


def _in(values: tuple[str, ...]) -> str:
    return f"kind IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_exports_kind"), "exports", type_="check")
    op.create_check_constraint(op.f("ck_exports_kind"), "exports", _in(P1 + P2))


def downgrade() -> None:
    op.execute(f"DELETE FROM exports WHERE kind IN ({', '.join(repr(v) for v in P2)})")
    op.drop_constraint(op.f("ck_exports_kind"), "exports", type_="check")
    op.create_check_constraint(op.f("ck_exports_kind"), "exports", _in(P1))
