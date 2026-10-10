"""Dashboard indexes on other domains' tables (Epic 11, A3 performance; FR-60 <= 3 s p95).

Documented cross-domain index exception (reporting is the read-model for dashboards): every index is created
``CONCURRENTLY`` (outside the migration transaction), only when its table exists (``to_regclass``) and no
index with the same columns and partial-ness exists already (e.g. ``ix_tasks_owner_status_due``).
Downgrade drops only the indexes created here, ``IF EXISTS``.

Revision ID: 20261001_112
Revises: 20261001_111
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261001_112"
down_revision: str | tuple[str, ...] | None = "20261001_111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, table, columns, partial predicate)
INDEXES: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("ix_rpt_applications_demand_stage", "applications", ("tenant_id", "demand_id", "stage_id"), None),
    ("ix_rpt_applications_created", "applications", ("tenant_id", "created_at"), None),
    ("ix_rpt_applications_source", "applications", ("tenant_id", "source"), None),
    ("ix_rpt_stage_history_to_at", "application_stage_history", ("tenant_id", "to_stage_id", "at"), None),
    ("ix_rpt_stage_history_application", "application_stage_history", ("tenant_id", "application_id"), None),
    ("ix_rpt_submissions_sent", "submissions", ("tenant_id", "sent_at"), "sent_at IS NOT NULL"),
    ("ix_rpt_submissions_sent_by", "submissions", ("tenant_id", "sent_by", "sent_at"), None),
    ("ix_rpt_submissions_demand_sent", "submissions", ("tenant_id", "demand_id", "sent_at"), None),
    ("ix_rpt_interviews_starts", "interviews", ("tenant_id", "starts_at"), None),
    ("ix_rpt_offers_status_released", "offers", ("tenant_id", "status", "released_at"), None),
    ("ix_rpt_demands_status_bu", "demands", ("tenant_id", "status", "business_unit_id"), None),
    ("ix_rpt_demands_opened", "demands", ("tenant_id", "opened_at"), None),
    ("ix_rpt_demand_assignments_user", "demand_assignments", ("tenant_id", "user_id"), "removed_at IS NULL"),
    (
        "ix_rpt_sla_clocks_open",
        "demand_sla_clocks",
        ("tenant_id", "demand_id", "status"),
        "completed_at IS NULL",
    ),
    ("ix_rpt_activities_author", "activities", ("tenant_id", "author_id", "occurred_at"), None),
    ("ix_rpt_tasks_owner_status_due", "tasks", ("tenant_id", "owner_id", "status", "due_at"), None),
)

_EXISTING = sa.text(
    """
    SELECT array_agg(a.attname::text ORDER BY k.ord) AS cols, i.indpred IS NOT NULL AS partial
    FROM pg_index i
    JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
    WHERE i.indrelid = to_regclass(:table)
    GROUP BY i.indexrelid, i.indpred
    """
)


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    return bool(
        bind.execute(sa.text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}).scalar()
    )


def _equivalent_exists(
    bind: sa.engine.Connection, table: str, cols: tuple[str, ...], where: str | None
) -> bool:
    for row in bind.execute(_EXISTING, {"table": f"public.{table}"}):
        if tuple(row.cols or ()) == cols and bool(row.partial) == (where is not None):
            return True
    return False


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, table, cols, where in INDEXES:
            if not _table_exists(bind, table) or _equivalent_exists(bind, table, cols, where):
                continue
            predicate = f" WHERE {where}" if where else ""
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({', '.join(cols)}){predicate}"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _cols, _where in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
