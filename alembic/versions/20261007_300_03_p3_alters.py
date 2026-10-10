# ruff: noqa: E501
"""Phase 3 foundation (Epic 36, story 36.3): shared P1/P2 table ALTERs (spec 0.3). Contract-layer head.

* ``demands``: ``visibility`` (external | internal | both, default external), ``workforce_plan_line_id`` ->
  ``workforce_plan_lines``, ``project_role_id`` -> ``project_roles`` (+ indexes).
* ``placements``: ``project_id`` -> ``projects``, ``project_role_id`` -> ``project_roles``; a role needs a project.
* ``contracts``: ``pricing_model`` (SOW only; legacy SOWs keep NULL). AD-39 "kind += sow" is the existing
  ``contract_type`` column, which already allows ``sow``.
* ``candidate_skills``: ``proficiency`` 1..5 and ``verification`` (declared | inferred | verified), backfilled from
  ``source``; the BEFORE INSERT trigger ``tos_skill_verification_default`` fills a NULL ``verification`` the same
  way so the P1 insert paths stay unchanged.
* ``interview_stages``: ``assessment_id`` -> ``assessments`` (only on ``kind = 'assessment'`` stages).

Every epic's first revision uses this revision as its ``down_revision``.

Revision ID: 20261007_300_03_p3_alters
Revises: 20261007_300_02_p3_hubs
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261007_300_03_p3_alters"
down_revision: str | tuple[str, ...] | None = "20261007_300_02_p3_hubs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SKILL_VERIFICATION_FN = r"""
CREATE OR REPLACE FUNCTION tos_skill_verification_default() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.verification IS NULL THEN
        NEW.verification := CASE WHEN NEW.source = 'parsed' THEN 'inferred' ELSE 'declared' END;
    END IF;
    RETURN NEW;
END
$fn$
"""


def _fk(table: str, column: str, target: str) -> None:
    op.create_foreign_key(
        f"fk_{table}_{column}_{target}",
        table,
        target,
        ["tenant_id", column],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- demands
    op.add_column(
        "demands",
        sa.Column("visibility", sa.String(10), server_default=sa.text("'external'"), nullable=False),
    )
    op.add_column("demands", sa.Column("workforce_plan_line_id", sa.Uuid(), nullable=True))
    op.add_column("demands", sa.Column("project_role_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        op.f("ck_demands_visibility"), "demands", "visibility IN ('external', 'internal', 'both')"
    )
    _fk("demands", "workforce_plan_line_id", "workforce_plan_lines")
    _fk("demands", "project_role_id", "project_roles")
    op.create_index("ix_demands_workforce_plan_line_id", "demands", ["workforce_plan_line_id"])
    op.create_index("ix_demands_project_role_id", "demands", ["project_role_id"])

    # ---------------------------------------------------------------- placements
    op.add_column("placements", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.add_column("placements", sa.Column("project_role_id", sa.Uuid(), nullable=True))
    _fk("placements", "project_id", "projects")
    _fk("placements", "project_role_id", "project_roles")
    op.create_check_constraint(
        op.f("ck_placements_project_role_project"),
        "placements",
        "project_role_id IS NULL OR project_id IS NOT NULL",
    )
    op.create_index("ix_placements_project_id", "placements", ["project_id"])
    op.create_index("ix_placements_project_role_id", "placements", ["project_role_id"])

    # ---------------------------------------------------------------- contracts
    op.add_column("contracts", sa.Column("pricing_model", sa.String(20), nullable=True))
    op.create_check_constraint(
        op.f("ck_contracts_pricing_model"),
        "contracts",
        "pricing_model IS NULL OR pricing_model IN ('time_and_materials', 'fixed_price', 'milestone')",
    )
    op.create_check_constraint(
        op.f("ck_contracts_pricing_model_sow"), "contracts", "contract_type = 'sow' OR pricing_model IS NULL"
    )

    # ---------------------------------------------------------------- candidate_skills
    op.add_column("candidate_skills", sa.Column("proficiency", sa.SmallInteger(), nullable=True))
    op.add_column("candidate_skills", sa.Column("verification", sa.String(10), nullable=True))
    op.execute(
        "UPDATE candidate_skills SET verification = CASE WHEN source = 'parsed' THEN 'inferred' ELSE 'declared' END"
    )
    op.alter_column("candidate_skills", "verification", nullable=False)
    op.create_check_constraint(
        op.f("ck_candidate_skills_proficiency"),
        "candidate_skills",
        "proficiency IS NULL OR proficiency BETWEEN 1 AND 5",
    )
    op.create_check_constraint(
        op.f("ck_candidate_skills_verification"),
        "candidate_skills",
        "verification IN ('declared', 'inferred', 'verified')",
    )
    op.execute(SKILL_VERIFICATION_FN)
    op.execute(
        "CREATE TRIGGER tos_skill_verification_default BEFORE INSERT ON candidate_skills "
        "FOR EACH ROW EXECUTE FUNCTION tos_skill_verification_default()"
    )

    # ---------------------------------------------------------------- interview_stages
    op.add_column("interview_stages", sa.Column("assessment_id", sa.Uuid(), nullable=True))
    _fk("interview_stages", "assessment_id", "assessments")
    op.create_check_constraint(
        op.f("ck_interview_stages_assessment_kind"),
        "interview_stages",
        "assessment_id IS NULL OR kind = 'assessment'",
    )
    op.create_index("ix_interview_stages_assessment_id", "interview_stages", ["assessment_id"])


def downgrade() -> None:
    op.drop_index("ix_interview_stages_assessment_id", table_name="interview_stages")
    op.drop_constraint(op.f("ck_interview_stages_assessment_kind"), "interview_stages", type_="check")
    op.drop_constraint(
        "fk_interview_stages_assessment_id_assessments", "interview_stages", type_="foreignkey"
    )
    op.drop_column("interview_stages", "assessment_id")

    op.execute("DROP TRIGGER IF EXISTS tos_skill_verification_default ON candidate_skills")
    op.execute("DROP FUNCTION IF EXISTS tos_skill_verification_default()")
    op.drop_constraint(op.f("ck_candidate_skills_verification"), "candidate_skills", type_="check")
    op.drop_constraint(op.f("ck_candidate_skills_proficiency"), "candidate_skills", type_="check")
    op.drop_column("candidate_skills", "verification")
    op.drop_column("candidate_skills", "proficiency")

    op.drop_constraint(op.f("ck_contracts_pricing_model_sow"), "contracts", type_="check")
    op.drop_constraint(op.f("ck_contracts_pricing_model"), "contracts", type_="check")
    op.drop_column("contracts", "pricing_model")

    op.drop_index("ix_placements_project_role_id", table_name="placements")
    op.drop_index("ix_placements_project_id", table_name="placements")
    op.drop_constraint(op.f("ck_placements_project_role_project"), "placements", type_="check")
    op.drop_constraint("fk_placements_project_role_id_project_roles", "placements", type_="foreignkey")
    op.drop_constraint("fk_placements_project_id_projects", "placements", type_="foreignkey")
    op.drop_column("placements", "project_role_id")
    op.drop_column("placements", "project_id")

    op.drop_index("ix_demands_project_role_id", table_name="demands")
    op.drop_index("ix_demands_workforce_plan_line_id", table_name="demands")
    op.drop_constraint("fk_demands_project_role_id_project_roles", "demands", type_="foreignkey")
    op.drop_constraint(
        "fk_demands_workforce_plan_line_id_workforce_plan_lines", "demands", type_="foreignkey"
    )
    op.drop_constraint(op.f("ck_demands_visibility"), "demands", type_="check")
    op.drop_column("demands", "project_role_id")
    op.drop_column("demands", "workforce_plan_line_id")
    op.drop_column("demands", "visibility")
