# ruff: noqa: E501
"""Epic 39 A4 / NFR-34 load test (``-m slow``): 50,000 messages (``MKT_LOAD_N`` overrides) through claim -> throttle ->
governance -> fake delivery. Asserts no duplicate delivery call even when every batch runs twice (retry), the token
bucket rate is held, and the throughput is >= 50,000 messages per hour."""

import asyncio
import os
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.campaigns import Campaign, CampaignMessage
from app.models.candidates import Candidate, CandidateEmail
from app.models.privacy import Consent
from app.services.campaigns import campaigns as campaign_svc
from app.services.campaigns import sending, throttle
from tests.modules.campaigns.helpers import audience, email_campaign, now, setup_marketing
from tests.modules.demands.helpers import DOrg

pytestmark = pytest.mark.slow
N = int(os.environ.get("MKT_LOAD_N", "50000"))


async def test_fifty_thousand_messages_never_double_send(dorg: DOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    m = dorg.admin
    await setup_marketing(m)
    aud = await audience(m, [])
    camp_id = uuid.UUID((await email_campaign(m, aud["id"]))["id"])
    tid = dorg.tenant_id
    ids = [uuid.uuid4() for _ in range(N)]
    async with tenant_session(tid) as db:
        owner = m.user_id
        for i in range(0, N, 5000):
            part = ids[i : i + 5000]
            await db.execute(Candidate.__table__.insert(), [{"id": c, "tenant_id": tid, "human_id": f"LD-{i + k}", "full_name": f"Load {i + k}", "source": "manual", "owner_id": owner} for k, c in enumerate(part)])
            await db.execute(CandidateEmail.__table__.insert(), [{"id": uuid.uuid4(), "tenant_id": tid, "candidate_id": c, "email": f"l{i + k}@example.com", "is_primary": True} for k, c in enumerate(part)])
            await db.execute(Consent.__table__.insert(), [{"id": uuid.uuid4(), "tenant_id": tid, "candidate_id": c, "purpose": "marketing", "status": "granted", "source": "recruiter_attestation", "granted_at": now()} for c in part])
    async with tenant_session(tid) as db:
        c = await db.get(Campaign, camp_id)
        assert c is not None
        c.status = "sending"
        msg_ids = await campaign_svc._insert_messages(db, c, ids, None)
    assert len(msg_ids) == N

    calls: list[uuid.UUID] = []

    async def fake_deliver(db: Any, cm: CampaignMessage, content: Any, address: str, *, sequence: bool) -> None:
        calls.append(cm.id)

    monkeypatch.setattr(sending, "deliver", fake_deliver)
    rate = 1000  # platform max per minute
    monkeypatch.setattr(throttle, "_override", None)
    throttle.memory_backend().reset()
    started = time.monotonic()
    batches = [msg_ids[i : i + 500] for i in range(0, N, 500)]

    async def run(batch: list[uuid.UUID]) -> sending.BatchResult:
        async with tenant_session(tid) as db:
            return await sending.send_batch(db, batch)

    # the token bucket holds back what exceeds the rate: re-run throttled ids after the refill, twice per batch (retry)
    pending = list(batches)
    rounds = 0
    while pending and rounds < 10_000:
        rounds += 1
        results = await asyncio.gather(*(run(b) for b in pending[:4]), *(run(b) for b in pending[:4]))
        throttled = {i for r in results for i in r.throttled}
        pending = [[i for i in b if i in throttled] for b in pending[:4] if any(i in throttled for i in b)] + pending[4:]
        if throttled:
            await asyncio.sleep(max(r.retry_after_ms for r in results) / 1000)
    elapsed = time.monotonic() - started
    assert len(calls) == N and len(set(calls)) == N  # no duplicate delivery call
    # token bucket (burst = rate, refill = rate/min): never more than the burst plus the refill, within 5 %;
    # exact for any N (a per-minute average is meaningless when N fits inside the initial burst)
    assert rate + rate * 1.05 * elapsed / 60 >= N, f"{N} sends in {elapsed:.1f}s exceed the bucket"
    assert N / elapsed * 3600 >= 50_000 or N < 5000
    async with tenant_session(tid) as db:
        queued = len(list((await db.execute(select(CampaignMessage.id).where(CampaignMessage.campaign_id == camp_id, CampaignMessage.status == "queued"))).scalars()))
    assert queued == N
