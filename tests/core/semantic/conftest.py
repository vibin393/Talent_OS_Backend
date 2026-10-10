"""Shared seed data for the semantic layer tests (Epic 36 story 36.2).

``world`` seeds two tenants once per session:

* tenant A: users u1 (team T, BU B), u2 (team T), u3 (BU B), u4; candidates c1..c4 owned by u1..u4, plus a
  merged (c5) and an erased (c6) candidate hidden by the base filter; tags, a tenant skill with
  proficiencies, search docs (expected compensation) and marketing consents;
  (the candidates' expected CTC matches the search docs, so a background projection refresh keeps them);
* tenant B: one candidate named like c1 (cross-tenant checks).
"""

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal, Scope
from app.core.semantic import SemanticContext
from app.models.candidates import Candidate, CandidateSkill, CandidateTag, Tag, TenantSkill
from app.models.identity import BusinessUnit, Team, TeamMember, User
from app.models.privacy import Consent
from app.models.search import CandidateSearchDoc
from app.models.skills_graph import CandidateSkillEvidence
from app.services.candidates import semantic_source as candidate_source
from app.services.placements import semantic_source as placement_source
from tests.factories import create_tenant_row

ALL = Scope.TENANT


@dataclasses.dataclass
class World:
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    users: dict[str, uuid.UUID]
    team_id: uuid.UUID
    bu_id: uuid.UUID
    c: dict[str, uuid.UUID]
    other_candidate: uuid.UUID
    skill_id: uuid.UUID
    other_skill_id: uuid.UUID
    tag_name: str

    def principal(self, user: str = "u1", **permissions: Scope) -> Principal:
        perms = permissions or {
            "candidate:read": ALL,
            "compensation:view": ALL,
            "placement:read": ALL,
            "bench:read": ALL,
            "identity:view": ALL,
        }
        return Principal(
            type="user",
            id=self.users[user],
            tenant_id=self.tenant_a,
            permissions={k.replace("__", ":"): v for k, v in perms.items()},
            team_ids=frozenset({self.team_id}) if user in {"u1", "u2"} else frozenset(),
            business_unit_id=self.bu_id if user in {"u1", "u3"} else None,
        )


def sctx(db: Any, principal: Principal | None, tenant_id: uuid.UUID) -> SemanticContext:
    return SemanticContext(RequestContext(tenant_id=tenant_id, principal=principal), db, "segment")


@pytest.fixture(scope="session", autouse=True)
def _base_sources() -> None:
    candidate_source.register()
    placement_source.register()


def _user(tenant_id: uuid.UUID, name: str, bu: uuid.UUID | None = None) -> User:
    return User(
        tenant_id=tenant_id,
        email=f"{name}-{uuid.uuid4().hex[:8]}@semantic.example.com",
        name=name,
        password_hash="x",
        business_unit_id=bu,
    )


def _candidate(tenant_id: uuid.UUID, owner: uuid.UUID, n: int, **kw: Any) -> Candidate:
    return Candidate(
        tenant_id=tenant_id,
        human_id=f"SEM-{uuid.uuid4().hex[:6]}-{n}",
        owner_id=owner,
        source=kw.pop("source", "manual"),
        **kw,
    )


def _doc(c: Candidate, comp: Decimal | None) -> CandidateSearchDoc:
    return CandidateSearchDoc(
        tenant_id=c.tenant_id,
        candidate_id=c.id,
        human_id=c.human_id,
        owner_id=c.owner_id,
        display_name=c.full_name,
        name_norm=c.full_name.lower(),
        source=c.source,
        candidate_updated_at=datetime.now(UTC),
        doc=func.to_tsvector("simple", c.full_name),
        expected_comp_inr=comp,
    )


@pytest.fixture(scope="session")
async def world() -> World:
    tenant_a = await create_tenant_row("Semantic A")
    tenant_b = await create_tenant_row("Semantic B")
    today = date.today()
    async with tenant_session(tenant_a) as db:
        bu = BusinessUnit(tenant_id=tenant_a, name=f"BU {uuid.uuid4().hex[:6]}")
        db.add(bu)
        await db.flush()
        users = {
            "u1": _user(tenant_a, "u1", bu.id),
            "u2": _user(tenant_a, "u2"),
            "u3": _user(tenant_a, "u3", bu.id),
            "u4": _user(tenant_a, "u4"),
        }
        db.add_all(users.values())
        await db.flush()
        team = Team(tenant_id=tenant_a, name=f"Team {uuid.uuid4().hex[:6]}")
        db.add(team)
        await db.flush()
        db.add_all(
            [
                TeamMember(tenant_id=tenant_a, team_id=team.id, user_id=users["u1"].id),
                TeamMember(tenant_id=tenant_a, team_id=team.id, user_id=users["u2"].id),
            ]
        )
        c1 = _candidate(
            tenant_a,
            users["u1"].id,
            1,
            full_name="Asha Rao",
            current_title="Data Engineer",
            current_employer="Infosys",
            total_experience_months=60,
            location={"city": "Chennai", "state": "Tamil Nadu", "country": "India"},
            notice_period_days=30,
            availability_date=today + timedelta(days=10),
            work_authorization="Indian citizen",
            willing_to_relocate=True,
            expected_ctc_amount=Decimal("1200000"),
            expected_ctc_currency="INR",
            expected_ctc_unit="annual",
        )
        c2 = _candidate(
            tenant_a,
            users["u2"].id,
            2,
            source="referral",
            full_name="Bala Kumar",
            current_title="QA Lead",
            total_experience_months=24,
            location={"city": "Bengaluru", "state": "Karnataka", "country": "India"},
            notice_period_days=90,
            archived_at=datetime.now(UTC),
            expected_ctc_amount=Decimal("2000000"),
            expected_ctc_currency="INR",
            expected_ctc_unit="annual",
        )
        c3 = _candidate(
            tenant_a,
            users["u3"].id,
            3,
            source="job_board",
            full_name="Chitra_100% Iyer",
            location={"city": "Chennai", "country": "India"},
        )
        c4 = _candidate(tenant_a, users["u4"].id, 4, full_name="Dev Patel", location={"city": "Pune"})
        db.add_all([c1, c2, c3, c4])
        await db.flush()
        c5 = _candidate(tenant_a, users["u1"].id, 5, full_name="Asha Rao (dup)", merged_into_id=c1.id)
        c6 = _candidate(tenant_a, users["u1"].id, 6, full_name="Erased Person", erased_at=datetime.now(UTC))
        db.add_all([c5, c6])
        skill = TenantSkill(
            tenant_id=tenant_a,
            name="Semantic Kotlin",
            slug=f"sk-{uuid.uuid4().hex[:6]}",
            status="approved",
            created_from="manual",
        )
        other = TenantSkill(
            tenant_id=tenant_a,
            name="Semantic Rust",
            slug=f"sr-{uuid.uuid4().hex[:6]}",
            status="approved",
            created_from="manual",
        )
        tag = Tag(tenant_id=tenant_a, name=f"java-guru-{uuid.uuid4().hex[:4]}")
        db.add_all([skill, other, tag])
        await db.flush()
        db.add_all(
            [
                CandidateSkill(
                    tenant_id=tenant_a,
                    candidate_id=c1.id,
                    tenant_skill_id=skill.id,
                    source="manual",
                    proficiency=4,
                ),
                CandidateSkill(
                    tenant_id=tenant_a, candidate_id=c2.id, tenant_skill_id=skill.id, source="manual"
                ),
                CandidateSkill(
                    tenant_id=tenant_a,
                    candidate_id=c3.id,
                    tenant_skill_id=skill.id,
                    source="manual",
                    proficiency=2,
                ),
                CandidateSkill(
                    tenant_id=tenant_a,
                    candidate_id=c4.id,
                    tenant_skill_id=other.id,
                    source="manual",
                    proficiency=5,
                ),
                CandidateTag(tenant_id=tenant_a, candidate_id=c1.id, tag_id=tag.id),
                _doc(c1, Decimal("1200000")),
                _doc(c2, Decimal("2000000")),
                Consent(
                    tenant_id=tenant_a,
                    candidate_id=c1.id,
                    purpose="marketing",
                    status="granted",
                    source="recruiter_attestation",
                    granted_at=datetime.now(UTC),
                ),
                Consent(
                    tenant_id=tenant_a,
                    candidate_id=c2.id,
                    purpose="marketing",
                    status="withdrawn",
                    source="recruiter_attestation",
                    withdrawn_at=datetime.now(UTC),
                ),
            ]
        )
        await db.flush()
        # Epic 37: proficiency is derived from evidence (the CandidateSkillsChanged baseline handler recomputes
        # skills without evidence to NULL), so back every seeded proficiency with a matching evidence row.
        seeded = (
            await db.execute(select(CandidateSkill).where(CandidateSkill.candidate_id.in_([c1.id, c2.id, c3.id, c4.id])))
        ).scalars()
        db.add_all(
            [
                CandidateSkillEvidence(
                    tenant_id=tenant_a,
                    candidate_skill_id=cs.id,
                    candidate_id=cs.candidate_id,
                    kind="self",
                    proficiency=cs.proficiency,
                    created_by_kind="system",
                )
                for cs in seeded
            ]
        )
        await db.flush()
        ids = {"c1": c1.id, "c2": c2.id, "c3": c3.id, "c4": c4.id, "c5": c5.id, "c6": c6.id}
        user_ids = {k: u.id for k, u in users.items()}
        team_id, bu_id, skill_id, other_id = team.id, bu.id, skill.id, other.id
        tag_name = tag.name
    async with tenant_session(tenant_b) as db:
        ub = _user(tenant_b, "ub")
        db.add(ub)
        await db.flush()
        cb = _candidate(tenant_b, ub.id, 1, full_name="Asha Rao", location={"city": "Chennai"})
        db.add(cb)
        await db.flush()
        other_candidate = cb.id
    return World(
        tenant_a, tenant_b, user_ids, team_id, bu_id, ids, other_candidate, skill_id, other_id, tag_name
    )
