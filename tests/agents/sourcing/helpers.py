# ruff: noqa: E501
"""Epic 56 test helpers: a seeded sourcing Demand and a Fake-LLM run of the ``sourcing`` agent.

``seed_sourcing`` (agent off while the Demand opens, then ``agentic`` + ``agent_sourcing`` on):

| key | candidate | match | state |
|---|---|---|---|
| a | Asha Rao | 90 | consents (AI + marketing), email |
| b | Bala Iyer | 80 | consents, email (created before c) |
| c | Chitra Nair | 80 | consents, email (later activity -> ranks before b) |
| d | Dev Kumar | -- | email, marketing, **no AI consent** (only findable by search) |
| e | Esha Pillai | 95 | already in the pipeline (excluded) |
| f | Farah Khan | 85 | archived (excluded) |
| g | Gita Menon | 70 | marketing withdrawn, no address (do not contact) |
"""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.candidates import Candidate
from app.services import ai
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.epic5 import h, make_candidate
from tests.modules.demands.helpers import create_client, open_demand
from tests.modules.identity.conftest import Member
from tests.modules.talent_pools.helpers import add_match

API = "/api/v1"
PAY_MIN = "2000000"
PAY_MAX = "3000000"


@dataclasses.dataclass
class Seed:
    tenant_id: uuid.UUID
    admin: Member
    demand_id: uuid.UUID
    c: dict[str, uuid.UUID]

    def values(self) -> dict[str, str]:
        out = {f"cand_{k}": str(v) for k, v in self.c.items()}
        out.update(
            demand_id=str(self.demand_id), subject_type="demand", subject_id=str(self.demand_id),
            owner_id=str(self.admin.user_id), random_id=str(uuid.uuid4()),
            subject_candidate_ids=",".join(str(v) for v in self.c.values()),
        )  # fmt: skip
        return out


async def grant(tenant_id: uuid.UUID, candidate_id: uuid.UUID, *purposes: str) -> None:
    from app.services.privacy import consent

    async with tenant_session(tenant_id) as db:
        for purpose in purposes:
            await consent.grant_consent(
                RequestContext(tenant_id=tenant_id), db, candidate_id, purpose, source="recruiter_attestation"
            )


async def withdraw(tenant_id: uuid.UUID, candidate_id: uuid.UUID, purpose: str) -> None:
    from app.services.privacy import consent

    async with tenant_session(tenant_id) as db:
        await consent.withdraw_consent(RequestContext(tenant_id=tenant_id), db, candidate_id, purpose)


async def enable_sourcing(tenant_id: uuid.UUID, on: bool = True) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, "agent_sourcing": on})


async def _cand(m: Member, name: str, email: bool = True) -> uuid.UUID:
    body: dict[str, Any] = {}
    if email:
        body["emails"] = [{"email": f"{uuid.uuid4().hex[:10]}@example.com", "is_primary": True}]
    return uuid.UUID((await make_candidate(m, name, **body))["id"])


async def _touch(tenant_id: uuid.UUID, candidate_id: uuid.UUID, days_ago: int) -> None:
    async with tenant_session(tenant_id) as db:
        await db.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(updated_at=datetime.now(UTC) - timedelta(days=days_ago))
        )


async def seed_sourcing(admin: Member, tenant_id: uuid.UUID) -> Seed:
    client_id = await create_client(admin)
    demand = await open_demand(
        admin, client_id, pay_rate_min=PAY_MIN, pay_rate_max=PAY_MAX, rate_unit="year", currency="INR"
    )
    demand_id = uuid.UUID(demand["id"])
    c: dict[str, uuid.UUID] = {}
    for key, name in (("a", "Asha Rao"), ("b", "Bala Iyer"), ("c", "Chitra Nair"), ("d", "Dev Kumar"),
                      ("e", "Esha Pillai"), ("f", "Farah Khan")):  # fmt: skip
        c[key] = await _cand(admin, name)
    c["g"] = await _cand(admin, "Gita Menon", email=False)
    for key in ("a", "b", "c", "e", "f", "g"):
        await grant(tenant_id, c[key], "ai_processing", "marketing")
    await grant(tenant_id, c["d"], "marketing")
    await withdraw(tenant_id, c["g"], "marketing")
    for key, score in (("a", "90"), ("b", "80"), ("c", "80"), ("e", "95"), ("f", "85"), ("g", "70")):
        await add_match(tenant_id, demand_id, c[key], score)
    await _touch(tenant_id, c["b"], 10)
    await _touch(tenant_id, c["c"], 2)
    resp = await admin.client.post(
        f"{API}/demands/{demand_id}/applications", json={"candidate_ids": [str(c["e"])]}, headers=h(admin)
    )
    assert resp.status_code in (200, 201), resp.text
    resp = await admin.client.post(f"{API}/candidates/{c['f']}/archive", headers=h(admin))
    assert resp.status_code == 200, resp.text
    await enable_sourcing(tenant_id)
    return Seed(tenant_id=tenant_id, admin=admin, demand_id=demand_id, c=c)


def turns(*raw: dict[str, Any]) -> list[FakeTurn]:
    return [FakeTurn.from_mapping(t) for t in raw]


async def run_agent(seed: Seed, script: list[dict[str, Any]]) -> Any:
    """Start a manual sourcing run with the Fake LLM playing ``script`` (eager Celery: it completes here)."""
    from app.services import entity_refs
    from app.services import sourcing_agent_service as svc

    async with tenant_session(seed.tenant_id) as db:
        principal = await entity_refs.principal_for_user(db, seed.tenant_id, seed.admin.user_id)
    ctx = RequestContext(tenant_id=seed.tenant_id, principal=principal)
    with fake_llm(turns(*script)):
        async with tenant_session(seed.tenant_id) as db:
            ref = await svc.start_manual_run(ctx, db, seed.demand_id, owner_user_id=seed.admin.user_id)
    return ref


async def rows(tenant_id: uuid.UUID, model: Any, *where: Any, order_by: Any = None) -> list[Any]:
    async with tenant_session(tenant_id) as db:
        stmt = select(model).where(*where)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        return list((await db.execute(stmt)).scalars())


GET = "shortlists.get_for_demand"
RANKED = "matching.list_ranked_matches"
SAVE = "shortlists.save_suggested"
DRAFT = "outreach.create_draft"
CAP = "messaging.cap_status"
SEARCH = "search.search_candidates"
DONE = {"kind": "emit_result", "result": {"outcome": "done", "summary": "Saved the shortlist."}}


def body(name: str = "Asha") -> str:
    return (
        f"Hi {name}, we are hiring a Senior Data Engineer in Chennai working with PySpark. "
        "Your background looks like a strong fit. Would you be open to a short call this week?"
    )


def happy_script(seed: Seed) -> list[dict[str, Any]]:
    d = str(seed.demand_id)
    return [
        {"kind": "tool_calls", "calls": [[GET, {"demand_id": d}], [RANKED, {"demand_id": d, "limit": 50}]]},
        {"kind": "tool_calls", "calls": [[SAVE, {"demand_id": d, "summary": "Three strong matches and one from search.",
                                               "candidates": [{"candidate_id": str(seed.c[k]), "sources": [{"kind": "search"}]} for k in ("d", "f")]}]]},
        {"kind": "tool_calls", "calls": [[CAP, {"demand_id": d, "candidate_ids": [str(seed.c["a"]), str(seed.c["c"])]}]]},
        {"kind": "tool_calls", "calls": [[DRAFT, {"demand_id": d, "drafts": [
            {"candidate_id": str(seed.c["a"]), "subject": "A data engineering role", "body": body("Asha")},
            {"candidate_id": str(seed.c["c"]), "subject": "A data engineering role", "body": body("Chitra")},
        ]}]]},
        DONE,
    ]  # fmt: skip


__all__ = [
    "Seed",
    "body",
    "enable_sourcing",
    "grant",
    "happy_script",
    "rows",
    "run_agent",
    "seed_sourcing",
    "turns",
    "withdraw",
]
