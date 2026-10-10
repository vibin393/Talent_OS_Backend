"""Epic 36 (spec 0.2 / 0.3, A5): Phase 3 hub tables and shared-table changes.

Tables, constraints and org RLS of ``talent_pools``, ``assessments``, ``assessment_invitations``, ``projects``,
``project_roles``, ``workforce_plan_lines``; the P1/P2 defaults that keep Phases 1-2 unchanged (``demands.visibility``,
the ``candidate_skills.verification`` trigger), ``contracts.pricing_model`` and the widened CHECKs.
"""

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import OrgScope, tenant_session
from app.models.assessments import Assessment, AssessmentInvitation
from app.models.candidates import CandidateSkill
from app.models.projects import Project, ProjectRole
from app.models.talent_pools import TalentPool
from app.models.workforce import WorkforcePlan, WorkforcePlanLine

HUBS = (
    "talent_pools",
    "assessments",
    "assessment_invitations",
    "projects",
    "project_roles",
    "workforce_plan_lines",
)


@dataclasses.dataclass
class Hub:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    client_a: uuid.UUID
    client_b: uuid.UUID
    cand_a: uuid.UUID
    cand_b: uuid.UUID


async def make_hub(app: Any) -> Hub:
    from tests.epic3 import create_candidate
    from tests.modules.demands.helpers import create_client
    from tests.modules.identity.conftest import join, make_tenant

    tenant = await make_tenant(seat_limit=10)
    admin = await join(app, tenant, name="P3 Hub Admin")
    try:
        client_a = await create_client(admin, "Client A")
        client_b = await create_client(admin, "Client B")
        cand_a = await create_candidate(admin, full_name="Asha Alpha")
        cand_b = await create_candidate(admin, full_name="Bala Beta")
    finally:
        await admin.client.aclose()
    return Hub(tenant.id, admin.user_id, client_a, client_b, uuid.UUID(cand_a["id"]), uuid.UUID(cand_b["id"]))


async def seed(h: Hub) -> dict[str, uuid.UUID]:
    ids = {
        k: uuid.uuid4()
        for k in ("pool", "asm", "inv_a", "inv_b", "prj_a", "prj_b", "role_a", "line_a", "line_n")
    }
    async with tenant_session(h.tenant_id) as db:
        db.add(
            TalentPool(
                id=ids["pool"], tenant_id=h.tenant_id, name="Java pool", kind="static", owner_id=h.user_id
            )
        )
        db.add(Assessment(id=ids["asm"], tenant_id=h.tenant_id, name="Java test", owner_id=h.user_id))
        for p, c in (("prj_a", h.client_a), ("prj_b", h.client_b)):
            db.add(Project(id=ids[p], tenant_id=h.tenant_id, human_id=f"PRJ-{p}", name=p, client_id=c))
        await db.flush()
        deadline = datetime.now(UTC) + timedelta(days=7)
        for k, cand in (("inv_a", h.cand_a), ("inv_b", h.cand_b)):
            db.add(
                AssessmentInvitation(
                    id=ids[k],
                    tenant_id=h.tenant_id,
                    assessment_id=ids["asm"],
                    candidate_id=cand,
                    context_kind="direct",
                    deadline_at=deadline,
                    invited_by_kind="user",
                    invited_by_id=h.user_id,
                )
            )
        # client_id is inherited from the project (tos_inherit_org); the value given here is overwritten.
        db.add(
            ProjectRole(
                id=ids["role_a"],
                tenant_id=h.tenant_id,
                project_id=ids["prj_a"],
                client_id=h.client_b,
                title="Dev",
            )
        )
        await db.flush()
        for k, c in (("line_a", h.client_a), ("line_n", None)):
            # Epic 44 (308_01): lines belong to a real plan and inherit its client (tos_inherit_org).
            plan_id = uuid.uuid4()
            db.add(
                WorkforcePlan(
                    id=plan_id,
                    tenant_id=h.tenant_id,
                    name=f"Plan {k}",
                    period_start=date(2026, 10, 1),
                    period_end=date(2027, 3, 31),
                    client_id=c,
                    currency="INR",
                    owner_id=h.user_id,
                )
            )
            await db.flush()
            db.add(
                WorkforcePlanLine(
                    id=ids[k],
                    tenant_id=h.tenant_id,
                    workforce_plan_id=plan_id,
                    client_id=c,
                    role_title="Dev",
                    period_start=date(2026, 11, 1),
                    period_end=date(2026, 12, 31),
                    headcount=2,
                    project_role_id=ids["role_a"] if k == "line_a" else None,
                )
            )
    return ids


async def _ids(h: Hub, table: str, org: OrgScope | None) -> set[uuid.UUID]:
    async with tenant_session(h.tenant_id, org=org) as db:
        return set((await db.execute(text(f"SELECT id FROM {table}"))).scalars())  # noqa: S608 - fixed names


async def test_hub_tables_exist_with_rls(owner_db: Any) -> None:
    async with owner_db.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:t) AND relkind = 'r'"
                ),
                {"t": list(HUBS)},
            )
        ).all()
        assert {r.relname for r in rows} == set(HUBS)
        assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows)
        org = set(
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_policies WHERE policyname = 'org_isolation' AND tablename = ANY(:t)"
                    ),
                    {"t": list(HUBS)},
                )
            ).scalars()
        )
        assert org == set(HUBS) - {"assessments"}
        uq = set(
            (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE indexname = 'uq_assessment_invitations_open'"
                    )
                )
            ).scalars()
        )
        assert uq == {"uq_assessment_invitations_open"}


async def test_org_rls_on_hubs(app: Any) -> None:
    h = await make_hub(app)
    ids = await seed(h)
    client_a, client_b = OrgScope("client", h.client_a), OrgScope("client", h.client_b)
    worker_a = OrgScope("worker", h.cand_a)

    # Internal (no org scope / internal) sees everything.
    assert await _ids(h, "projects", None) == {ids["prj_a"], ids["prj_b"]}
    assert await _ids(h, "projects", OrgScope("internal")) == {ids["prj_a"], ids["prj_b"]}
    # Client sessions see only their client's projects, roles and plan lines (NULL client = internal only).
    assert await _ids(h, "projects", client_a) == {ids["prj_a"]}
    assert await _ids(h, "projects", client_b) == {ids["prj_b"]}
    assert await _ids(h, "project_roles", client_a) == {ids["role_a"]}
    assert await _ids(h, "project_roles", client_b) == set()
    assert await _ids(h, "workforce_plan_lines", client_a) == {ids["line_a"]}
    assert await _ids(h, "workforce_plan_lines", None) == {ids["line_a"], ids["line_n"]}
    # Worker sessions see only their own invitations; pools are internal only.
    assert await _ids(h, "assessment_invitations", worker_a) == {ids["inv_a"]}
    assert await _ids(h, "assessment_invitations", client_a) == set()
    assert await _ids(h, "talent_pools", client_a) == set()
    assert await _ids(h, "talent_pools", worker_a) == set()
    assert await _ids(h, "talent_pools", None) == {ids["pool"]}
    # Assessments: tenant RLS only (the candidate portal reads the name).
    assert await _ids(h, "assessments", worker_a) == {ids["asm"]}

    async with tenant_session(h.tenant_id) as db:
        inherited = (
            await db.execute(text("SELECT client_id FROM project_roles WHERE id = :i"), {"i": ids["role_a"]})
        ).scalar_one()
    assert inherited == h.client_a


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "UPDATE talent_pools SET segment_id = gen_random_uuid() WHERE id = :pool",
            "ck_talent_pools_segment_dynamic",
        ),
        (
            "UPDATE assessment_invitations SET score = 101 WHERE id = :inv_a",
            "ck_assessment_invitations_score",
        ),
        (
            "UPDATE assessment_invitations SET status = 'passed' WHERE id = :inv_a",
            "ck_assessment_invitations_status_result",
        ),
        (
            "UPDATE assessment_invitations SET result = 'pass' WHERE id = :inv_a",
            "ck_assessment_invitations_status_result",
        ),
        (
            "UPDATE assessment_invitations SET context_kind = 'application' WHERE id = :inv_a",
            "ck_assessment_invitations_application_ctx",
        ),
        (
            "UPDATE projects SET start_date = '2026-12-01', end_date = '2026-11-01' WHERE id = :prj_a",
            "ck_projects_dates",
        ),
        ("UPDATE project_roles SET headcount = 0 WHERE id = :role_a", "ck_project_roles_headcount"),
        (
            "UPDATE workforce_plan_lines SET period_end = '2026-01-01' WHERE id = :line_a",
            "ck_workforce_plan_lines_period",
        ),
    ],
)
async def test_hub_checks(app: Any, sql: str, constraint: str) -> None:
    h = await make_hub(app)
    ids = await seed(h)
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(h.tenant_id) as db:
            await db.execute(text(sql), {k: v for k, v in ids.items() if f":{k}" in sql})
    assert constraint in str(exc.value)


async def test_one_open_invitation_and_valid_outcome(app: Any) -> None:
    h = await make_hub(app)
    ids = await seed(h)
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(h.tenant_id) as db:
            await db.execute(
                text(
                    "INSERT INTO assessment_invitations (id, tenant_id, assessment_id, candidate_id, context_kind, "
                    "deadline_at, invited_by_kind) VALUES (gen_random_uuid(), :t, :a, :c, 'direct', now(), 'user')"
                ),
                {"t": h.tenant_id, "a": ids["asm"], "c": h.cand_a},
            )
    assert "uq_assessment_invitations_open" in str(exc.value)
    async with tenant_session(h.tenant_id) as db:
        await db.execute(
            text(
                "UPDATE assessment_invitations SET status = 'passed', result = 'pass', score = 80 WHERE id = :i"
            ),
            {"i": ids["inv_a"]},
        )


async def test_shared_defaults_and_skill_verification_trigger(app: Any) -> None:
    from tests.modules.demands.helpers import create_client  # noqa: F401 - keeps the helper import path warm

    h = await make_hub(app)
    async with tenant_session(h.tenant_id) as db:
        skill_ids = list((await db.execute(text("SELECT id FROM skills ORDER BY slug LIMIT 3"))).scalars())
        if len(skill_ids) < 3:  # pragma: no cover - catalogue seeded by P1 migrations
            pytest.skip("no platform skills")
        # P1 insert paths never set verification: the trigger fills it from source.
        db.add(
            CandidateSkill(
                id=uuid.uuid4(),
                tenant_id=h.tenant_id,
                candidate_id=h.cand_a,
                skill_id=skill_ids[0],
                source="parsed",
            )
        )
        db.add(
            CandidateSkill(
                id=uuid.uuid4(),
                tenant_id=h.tenant_id,
                candidate_id=h.cand_a,
                skill_id=skill_ids[1],
                source="manual",
            )
        )
        db.add(
            CandidateSkill(
                id=uuid.uuid4(),
                tenant_id=h.tenant_id,
                candidate_id=h.cand_a,
                skill_id=skill_ids[2],
                source="manual",
                verification="verified",
                proficiency=4,
            )
        )
        await db.flush()
        got = dict(
            (
                await db.execute(
                    text("SELECT skill_id, verification FROM candidate_skills WHERE candidate_id = :c"),
                    {"c": h.cand_a},
                )
            )
            .tuples()
            .all()
        )
    assert got == {skill_ids[0]: "inferred", skill_ids[1]: "declared", skill_ids[2]: "verified"}
    with pytest.raises(DBAPIError):
        async with tenant_session(h.tenant_id) as db:
            await db.execute(
                text("UPDATE candidate_skills SET proficiency = 6 WHERE candidate_id = :c"), {"c": h.cand_a}
            )


async def test_contract_pricing_model_only_on_sow(owner_db: Any) -> None:
    async with owner_db.connect() as conn:
        defs = dict(
            (
                await conn.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN ('ck_contracts_pricing_model', 'ck_contracts_pricing_model_sow', "
                        "'ck_demands_visibility', 'ck_placements_project_role_project', "
                        "'ck_interview_stages_assessment_kind')"
                    )
                )
            )
            .tuples()
            .all()
        )
        assert set(defs) == {
            "ck_contracts_pricing_model",
            "ck_contracts_pricing_model_sow",
            "ck_demands_visibility",
            "ck_placements_project_role_project",
            "ck_interview_stages_assessment_kind",
        }
        assert "sow" in defs["ck_contracts_pricing_model_sow"]
        default = (
            await conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'demands' AND column_name = 'visibility'"
                )
            )
        ).scalar_one()
        assert "external" in default


async def test_widened_checks_accept_phase3_values(owner_db: Any) -> None:
    expected = {
        "ck_consents_purpose": (
            "assessment",
            "demographic_self_id",
            "referral",
            "campus",
            "immigration",
            "tracking",
        ),
        "ck_consent_notices_purpose": ("assessment", "tracking"),
        "ck_consents_source": ("campus_registration", "event_checkin", "referral_link", "self_id_form"),
        "ck_provider_connector_configs_category": ("assessment", "dsc", "tds_statement", "fx"),
        "ck_connector_jobs_category": ("assessment", "dsc", "tds_statement", "fx"),
        "ck_connector_inbound_events_category": ("assessment", "fx"),
        "ck_candidates_source": ("campus", "event", "internal", "referral"),
        "ck_applications_source": ("internal", "referral", "campaign", "campus", "event", "vendor"),
        "ck_interview_stages_kind": ("assessment",),
        "ck_channel_templates_purpose": ("marketing",),
        "ck_email_messages_origin": ("campaign",),
        "ck_channel_messages_origin": ("campaign",),
        "ck_tasks_entity_type": (
            "offer",
            "placement",
            "project",
            "talent_pool",
            "campaign",
            "workforce_plan",
        ),
        "ck_exports_kind": ("talent_pools", "report_definitions", "workforce_plans", "bgv_cases"),
    }
    async with owner_db.connect() as conn:
        defs = dict(
            (
                await conn.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = ANY(:n)"
                    ),
                    {"n": list(expected)},
                )
            )
            .tuples()
            .all()
        )
    assert set(defs) == set(expected)
    for name, values in expected.items():
        for v in values:
            assert f"'{v}'" in defs[name], (name, v)
