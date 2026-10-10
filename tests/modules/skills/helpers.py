"""Epic 37 test helpers."""

import uuid
from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.candidates import CandidateSkill, Skill
from tests.epic5 import h, make_candidate
from tests.modules.identity.conftest import Member

GRAPH = "/api/v1/skill-graph"
CANDIDATES = "/api/v1/candidates"


async def skill_id(name: str, tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        return (await db.execute(select(Skill.id).where(Skill.name == name))).scalar_one()


async def gkey(name: str, tenant_id: uuid.UUID) -> str:
    return f"global:{await skill_id(name, tenant_id)}"


async def candidate_with(m: Member, name: str, skills: list[Any], **body: Any) -> dict[str, Any]:
    return await make_candidate(m, name, skills=skills, **body)


async def candidate_skill_ids(tenant_id: uuid.UUID, candidate_id: str) -> dict[str, uuid.UUID]:
    async with tenant_session(tenant_id) as db:
        rows = await db.execute(
            select(Skill.name, CandidateSkill.id)
            .join(Skill, Skill.id == CandidateSkill.skill_id)
            .where(CandidateSkill.candidate_id == uuid.UUID(candidate_id))
        )
        return {str(r[0]): r[1] for r in rows.all()}


async def add_edge(m: Member, from_key: str, to_key: str, kind: str, weight: Any = "0.8") -> Any:
    return await m.client.post(
        f"{GRAPH}/edges",
        json={"from_key": from_key, "to_key": to_key, "kind": kind, "weight": weight},
        headers=h(m),
    )
