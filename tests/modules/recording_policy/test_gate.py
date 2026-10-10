"""Epic 53 (spec 0.13, FR-294, coordinator decision 9): the Recording Policy gate and its per-tenant seed."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.core.provisioning import provision_tenant
from app.models.recording_policy import RecordingPolicy
from app.services.privacy import consent
from app.services.recording_policy import check, seed_tenant_defaults
from tests.epic3 import Org3, create_candidate
from tests.modules.identity.conftest import make_tenant


def ctx(org: Org3) -> RequestContext:
    principal = Principal(
        type="user",
        id=org.admin.user_id,
        tenant_id=org.tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


async def _candidate(org: Org3, **location: Any) -> uuid.UUID:
    body = {"location": location} if location else {}
    return uuid.UUID((await create_candidate(org.admin, **body))["id"])


async def _seed(org: Org3, *extra: tuple[str, str | None, str]) -> None:
    async with tenant_session(org.tenant_id) as db:
        await seed_tenant_defaults(db)
        await seed_tenant_defaults(db)  # idempotent
        for country, region, rule in extra:
            db.add(RecordingPolicy(tenant_id=org.tenant_id, country=country, region=region, rule=rule))


async def _grant(org: Org3, cand: uuid.UUID, purpose: str) -> None:
    async with tenant_session(org.tenant_id) as db:
        await consent.record(db, cand, purpose, "conversation", None, RequestContext(tenant_id=org.tenant_id))


async def test_india_with_consent_is_allowed_without_is_not(org3: Org3) -> None:
    await _seed(org3)
    cand = await _candidate(org3, city="Chennai", state="Tamil Nadu", country="India")
    async with tenant_session(org3.tenant_id) as db:
        d = await check(ctx(org3), db, purpose="call_recording", candidate_id=cand)
    assert (d.recording_allowed, d.rule, d.jurisdiction) == (False, "candidate_consent", "IN-TAMIL NADU")
    assert d.reasons == ("missing_consent:call_recording",) and d.required == ("consent:call_recording",)
    await _grant(org3, cand, "call_recording")
    async with tenant_session(org3.tenant_id) as db:
        d = await check(ctx(org3), db, purpose="call_recording", candidate_id=cand)
        assert (d.recording_allowed, d.rule, d.reasons) == (True, "candidate_consent", ())
        # another purpose still needs its own consent
        d2 = await check(ctx(org3), db, purpose="interview_recording", candidate_id=cand)
        assert d2.reasons == ("missing_consent:interview_recording",)


async def test_unlisted_country_is_not_permitted(org3: Org3) -> None:
    await _seed(org3)
    cand = await _candidate(org3, country="United States")
    await _grant(org3, cand, "call_recording")
    async with tenant_session(org3.tenant_id) as db:
        d = await check(ctx(org3), db, purpose="call_recording", candidate_id=cand)
        assert (d.recording_allowed, d.rule, d.jurisdiction, d.reasons) == (
            False,
            "not_permitted",
            "US",
            ("not_permitted",),
        )
        # a confirmed jurisdiction from the consent page wins over the profile
        d = await check(
            ctx(org3), db, purpose="call_recording", candidate_id=cand, confirmed_jurisdiction=("IN", None)
        )
        assert (d.recording_allowed, d.jurisdiction) == (True, "IN")


async def test_unknown_location_is_not_permitted(org3: Org3) -> None:
    await _seed(org3)
    cand = await _candidate(org3)
    await _grant(org3, cand, "call_recording")
    async with tenant_session(org3.tenant_id) as db:
        d = await check(ctx(org3), db, purpose="call_recording", candidate_id=cand)
    assert (d.recording_allowed, d.rule, d.jurisdiction) == (False, "unknown", None)
    assert "unknown_jurisdiction" in d.reasons


async def test_region_row_wins_and_all_party_needs_every_ack(org3: Org3) -> None:
    await _seed(org3, ("US", None, "not_permitted"), ("US", "CA", "all_party_consent"))
    cand = await _candidate(org3, state="ca", country="US")
    await _grant(org3, cand, "interview_recording")
    a, b = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(org3.tenant_id) as db:
        d = await check(
            ctx(org3),
            db,
            purpose="interview_recording",
            candidate_id=cand,
            participant_user_ids=(a, b),
            acks={a: now},
        )
        assert (d.recording_allowed, d.rule, d.jurisdiction) == (False, "all_party_consent", "US-CA")
        assert d.reasons == (f"missing_ack:{b}",)
        assert d.required == ("consent:interview_recording", f"ack:{a}", f"ack:{b}")
        d = await check(
            ctx(org3),
            db,
            purpose="interview_recording",
            candidate_id=cand,
            participant_user_ids=(a, b),
            acks={a: now, b: now},
        )
        assert d.recording_allowed is True and d.reasons == ()


async def test_seed_exists_per_tenant_via_provisioning() -> None:
    for _ in range(2):
        tenant = await make_tenant()
        async with tenant_session(tenant.id) as db:
            done = await provision_tenant(db, tenant)
            assert "recording_policy.defaults" in done
        async with tenant_session(tenant.id) as db:
            rows = (await db.execute(select(RecordingPolicy))).scalars().all()
            assert [(r.country, r.region, r.rule) for r in rows] == [("IN", None, "candidate_consent")]
            assert all(r.tenant_id == tenant.id for r in rows)
            await seed_tenant_defaults(db)
            assert len((await db.execute(select(RecordingPolicy))).scalars().all()) == 1
