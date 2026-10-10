# ruff: noqa: E501
"""Epic 61 test helpers: a shortlisted Application ready for a submission pack.

``seed_pack(admin, tenant_id)`` builds, through the API: a Client with one contact, an Open Demand (contract, INR per
hour, bill 1500, pay max 1100, submission limit 5), a Candidate (notice 30 days, expected 1000 INR / hour) with a parsed
resume, and the Application moved to Shortlisted by the admin (a person). The Submission Agent stays off unless
``enable_agent`` is called, so shortlisting starts no run.
"""

import dataclasses
import uuid
from typing import Any

from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.services import ai
from tests.modules.demands.helpers import create_client, open_demand
from tests.modules.identity.conftest import Member
from tests.modules.pipeline.helpers import (
    add_one,
    create_candidate,
    create_contact,
    h,
    shortlist,
    upload_parsed_resume,
)

API = "/api/v1"


@dataclasses.dataclass
class PackSeed:
    tenant_id: uuid.UUID
    admin: Member
    client_id: uuid.UUID
    contact_id: uuid.UUID
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID

    def values(self) -> dict[str, str]:
        return {
            "application_id": str(self.application_id), "candidate_id": str(self.candidate_id),
            "demand_id": str(self.demand_id), "client_id": str(self.client_id), "owner_id": str(self.admin.user_id),
            "subject_type": "application", "subject_id": str(self.application_id),
            "subject_candidate_ids": str(self.candidate_id), "random_id": str(uuid.uuid4()),
        }  # fmt: skip


async def set_candidate(tenant_id: uuid.UUID, candidate_id: uuid.UUID, **values: Any) -> None:
    from app.models.candidates import Candidate

    async with tenant_session(tenant_id) as db:
        await db.execute(update(Candidate).where(Candidate.id == candidate_id).values(**values))


async def seed_pack(
    admin: Member,
    tenant_id: uuid.UUID,
    *,
    parsed: bool = True,
    name: str = "Priya Raman",
    demand: dict[str, Any] | None = None,
) -> PackSeed:
    client_id = await create_client(admin)
    contact_id, _ = await create_contact(admin, client_id)
    d = await open_demand(admin, client_id, submission_limit=5, **(demand or {}))
    cand = await create_candidate(admin, name)
    candidate_id = uuid.UUID(cand["id"])
    await set_candidate(
        tenant_id,
        candidate_id,
        expected_ctc_amount=1000,
        expected_ctc_currency="INR",
        expected_ctc_unit="hourly",
    )
    await upload_parsed_resume(admin, tenant_id, cand["id"], parsed=parsed)
    app_id = await add_one(admin, str(d["id"]), cand["id"])
    await shortlist(admin, app_id)
    return PackSeed(
        tenant_id=tenant_id, admin=admin, client_id=client_id, contact_id=contact_id, demand_id=uuid.UUID(str(d["id"])),
        candidate_id=candidate_id, application_id=uuid.UUID(app_id),
    )  # fmt: skip


async def enable_agent(tenant_id: uuid.UUID, on: bool = True) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_features(db, tenant_id, {"agentic": True, "agent_submission": on})


async def grant_submission_consent(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, client_id: uuid.UUID
) -> None:
    from app.services.privacy import consent

    async with tenant_session(tenant_id) as db:
        await consent.grant_consent(
            RequestContext(tenant_id=tenant_id), db, candidate_id, "client_submission",
            source="recruiter_attestation", client_id=client_id,
        )  # fmt: skip


async def put_rules(seed: PackSeed, **rules: Any) -> dict[str, Any]:
    resp = await seed.admin.client.put(
        f"{API}/clients/{seed.client_id}/submission-rules", json=rules, headers=h(seed.admin)
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def prepare(seed: PackSeed, member: Member | None = None) -> dict[str, Any]:
    m = member or seed.admin
    resp = await m.client.post(f"{API}/applications/{seed.application_id}/submission-pack", headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def get_pack(seed: PackSeed, member: Member | None = None) -> Any:
    m = member or seed.admin
    return await m.client.get(f"{API}/applications/{seed.application_id}/submission-pack")


def check_of(pack: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in pack["checks"] if c["key"] == key)


async def pack_rows(tenant_id: uuid.UUID, *where: Any) -> list[Any]:
    from app.models.submission_packs import SubmissionPack

    async with tenant_session(tenant_id) as db:
        return list((await db.execute(select(SubmissionPack).where(*where))).scalars())


__all__ = [
    "API",
    "PackSeed",
    "check_of",
    "enable_agent",
    "get_pack",
    "grant_submission_consent",
    "pack_rows",
    "prepare",
    "put_rules",
    "seed_pack",
    "set_candidate",
]
