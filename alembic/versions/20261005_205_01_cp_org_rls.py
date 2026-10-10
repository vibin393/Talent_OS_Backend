# ruff: noqa: E501
"""Epic 18 (Client Portal) 205_01: org RLS on the Phase 1 hiring tables (AD-26, A1) and Client demand requests.

* ``demands``: custom org policy -- internal, the demand's client, or a worker (candidate portal) that has an
  application on it (P1 candidate portal keeps reading its demands' titles).
* ``submissions``: client (``client_id``) and worker (``candidate_id``).
* ``client_feedback``: new ``client_id`` inherited from ``submissions``.
* ``interviews``: new ``client_id`` inherited from ``demands``; client + worker (``candidate_id``).
* ``interview_participants`` / ``scorecards``: new ``client_id`` inherited from ``interviews``.
* ``demands.requested_by_client_user_id`` (FR-100) and ``client_feedback.source`` widened with ``portal``.

Internal sessions run as ``org_kind='internal'``: Phase 1 behaviour is unchanged.

Revision ID: 20261005_205_01_cp_org_rls
Revises: 20261002_200_07
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_helpers import ORG_POLICY_NAME, InheritRef, disable_org_rls, enable_org_rls

revision: str = "20261005_205_01_cp_org_rls"
down_revision: str | tuple[str, ...] | None = "20261002_200_07"
branch_labels: str | Sequence[str] | None = ("p2_client_portal",)
depends_on: str | Sequence[str] | None = "20261003_201_03_external_auth"

_KIND = "current_setting('app.org_kind', true)"
_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
DEMANDS_PREDICATE = (
    f"({_KIND} = 'internal' OR ({_KIND} = 'client' AND demands.client_id = {_ORG}) OR "
    f"({_KIND} = 'worker' AND EXISTS (SELECT 1 FROM applications a WHERE a.demand_id = demands.id "
    f"AND a.candidate_id = {_ORG})))"
)
CHILDREN: tuple[tuple[str, str, str], ...] = (
    ("client_feedback", "submissions", "submission_id"),
    ("interviews", "demands", "demand_id"),
    ("interview_participants", "interviews", "interview_id"),
    ("scorecards", "interviews", "interview_id"),
)


def upgrade() -> None:
    op.add_column("demands", sa.Column("requested_by_client_user_id", sa.Uuid(), nullable=True))
    op.execute(
        "ALTER TABLE demands ADD CONSTRAINT fk_demands_requested_by_client_user_id_client_users "
        "FOREIGN KEY (requested_by_client_user_id) REFERENCES client_users (id) ON DELETE SET NULL"
    )
    op.drop_constraint(op.f("ck_client_feedback_source"), "client_feedback", type_="check")
    op.create_check_constraint(
        op.f("ck_client_feedback_source"), "client_feedback", "source IN ('link', 'recorded', 'portal')"
    )
    for table, parent, fk in CHILDREN:
        op.add_column(table, sa.Column("client_id", sa.Uuid(), nullable=True))
        op.execute(f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p WHERE p.id = t.{fk}")
        op.create_index(f"ix_{table}_client_id", table, ["client_id"])

    op.execute(f"DROP POLICY IF EXISTS {ORG_POLICY_NAME} ON demands")
    op.execute(
        f"CREATE POLICY {ORG_POLICY_NAME} ON demands AS RESTRICTIVE FOR ALL "
        f"USING {DEMANDS_PREDICATE} WITH CHECK {DEMANDS_PREDICATE}"
    )
    enable_org_rls("submissions", client_col="client_id", worker_col="candidate_id")
    enable_org_rls(
        "client_feedback", client_col="client_id", inherit_from=InheritRef("submissions", "submission_id", ("client_id",))
    )
    enable_org_rls(
        "interviews",
        client_col="client_id",
        worker_col="candidate_id",
        inherit_from=InheritRef("demands", "demand_id", ("client_id",)),
    )
    for table in ("interview_participants", "scorecards"):
        enable_org_rls(
            table, client_col="client_id", inherit_from=InheritRef("interviews", "interview_id", ("client_id",))
        )


def downgrade() -> None:
    for table in ("scorecards", "interview_participants", "interviews", "client_feedback", "submissions"):
        disable_org_rls(table)
    op.execute(f"DROP POLICY IF EXISTS {ORG_POLICY_NAME} ON demands")
    for table, _parent, _fk in reversed(CHILDREN):
        op.drop_index(f"ix_{table}_client_id", table_name=table)
        op.drop_column(table, "client_id")
    op.execute("DELETE FROM client_feedback WHERE source = 'portal'")
    op.drop_constraint(op.f("ck_client_feedback_source"), "client_feedback", type_="check")
    op.create_check_constraint(op.f("ck_client_feedback_source"), "client_feedback", "source IN ('link', 'recorded')")
    op.execute("ALTER TABLE demands DROP CONSTRAINT IF EXISTS fk_demands_requested_by_client_user_id_client_users")
    op.drop_column("demands", "requested_by_client_user_id")
