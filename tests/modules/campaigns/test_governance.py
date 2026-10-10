# ruff: noqa: E501
"""Epic 39 story 39.2 (FR-201, AD-43): the single governance decision, caps, windows, throttle."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from app.core.database import tenant_session
from app.models.campaigns import CampaignMessage
from app.models.candidates import Candidate
from app.services import comms_service
from app.services.campaigns import governance, throttle, tokens
from app.services.campaigns import settings as mkt_settings
from tests.epic5 import h
from tests.modules.campaigns.helpers import (
    API,
    audience,
    bare_campaign,
    candidate,
    grant,
    insert_message,
    now,
)
from tests.modules.demands.helpers import DOrg

IST = ZoneInfo("Asia/Kolkata")


async def _check(
    tenant_id: uuid.UUID, cid: uuid.UUID, channel: str = "email", at: datetime | None = None
) -> governance.Decision:
    async with tenant_session(tenant_id) as db:
        address = await governance.address_for(db, cid, channel)
        return await governance.check(db, candidate_id=cid, channel=channel, address=address, now=at or now())


async def test_consent_opt_out_bounce_complaint_and_address(dorg: DOrg) -> None:
    tid = dorg.tenant_id
    ok = await candidate(dorg.admin, "Ok")
    assert isinstance(await _check(tid, ok), governance.Send)

    no_consent = await candidate(dorg.admin, "Nope", consents=())
    assert await _check(tid, no_consent) == governance.Suppress("no_marketing_consent")

    no_email = await candidate(dorg.admin, "Mute", email=None)
    assert await _check(tid, no_email) == governance.Suppress("no_address")

    opted = await candidate(dorg.admin, "Opted", email="opted@example.com")
    async with tenant_session(tid) as db:
        await comms_service.set_marketing_opt_out(db, "opted@example.com")
    assert await _check(tid, opted) == governance.Suppress("channel_opt_out")

    bounced = await candidate(dorg.admin, "Bounced", email="bounce@example.com")
    async with tenant_session(tid) as db:
        await comms_service.record_bounce(db, "bounce@example.com", None, "hard", "mailbox unknown")
    assert await _check(tid, bounced) == governance.Suppress("hard_bounce")

    sms_no_phone = await candidate(dorg.admin, "Phoneless")
    assert await _check(tid, sms_no_phone, "sms") == governance.Suppress("no_address")
    wa = await candidate(dorg.admin, "Wa", phone="+919876500001")
    assert await _check(tid, wa, "whatsapp") == governance.Suppress("no_whatsapp_optin")
    await grant(tid, wa, "whatsapp")
    assert isinstance(await _check(tid, wa, "whatsapp"), governance.Send)

    erased = await candidate(dorg.admin, "Gone")
    async with tenant_session(tid) as db:
        await db.execute(update(Candidate).where(Candidate.id == erased).values(erased_at=now()))
    assert await _check(tid, erased) == governance.Suppress("erased")


async def test_frequency_cap_three_per_seven_days_across_campaigns_and_sequences(dorg: DOrg) -> None:
    tid = dorg.tenant_id
    cid = await candidate(dorg.admin, "Capped")
    aud = await audience(dorg.admin, [cid])
    c1, c2, c3 = [await bare_campaign(dorg.admin, aud["id"]) for _ in range(3)]
    await insert_message(tid, cid, campaign_id=c1, status="sent")
    await insert_message(tid, cid, campaign_id=c2, status="delivered", channel="sms")
    await insert_message(tid, cid, campaign_id=c3, status="suppressed")  # never counted
    assert isinstance(await _check(tid, cid), governance.Send)  # 2 counted
    c4 = await bare_campaign(dorg.admin, aud["id"])
    await insert_message(
        tid, cid, campaign_id=c4, status="sent", sent_at=now() - timedelta(days=8)
    )  # outside 7 days
    assert isinstance(await _check(tid, cid), governance.Send)
    c5 = await bare_campaign(dorg.admin, aud["id"])
    await insert_message(tid, cid, campaign_id=c5, status="queued")
    assert await _check(tid, cid) == governance.Suppress("frequency_cap")


async def test_concurrent_sends_at_count_two_only_one_passes(dorg: DOrg) -> None:
    tid = dorg.tenant_id
    cid = await candidate(dorg.admin, "Race")
    aud = await audience(dorg.admin, [cid])
    camps = [await bare_campaign(dorg.admin, aud["id"]) for _ in range(4)]
    await insert_message(tid, cid, campaign_id=camps[0])
    await insert_message(tid, cid, campaign_id=camps[1])

    async def attempt(campaign_id: uuid.UUID) -> bool:
        async with tenant_session(tid) as db:
            address = await governance.address_for(db, cid, "email")
            decision = await governance.check(
                db, candidate_id=cid, channel="email", address=address, now=now()
            )
            if not isinstance(decision, governance.Send):
                return False
            await asyncio.sleep(0.2)  # hold the advisory lock while "sending"
            db.add(
                CampaignMessage(
                    id=uuid.uuid4(), tenant_id=tid, campaign_id=campaign_id, candidate_id=cid, channel="email",
                    status="queued", sent_at=now(), token_nonce=tokens.new_nonce(),
                )
            )  # fmt: skip
            return True

    results = await asyncio.gather(attempt(camps[2]), attempt(camps[3]))
    assert sorted(results) == [False, True]


@pytest.mark.real_windows
async def test_quiet_hours_defer_to_next_window_with_jitter(dorg: DOrg) -> None:
    tid = dorg.tenant_id
    cid = await candidate(dorg.admin, "Early", country="IN", phone="+919876500002")
    at = datetime(2026, 10, 8, 7, 30, tzinfo=IST).astimezone(UTC)
    decision = await _check(tid, cid, "email", at)
    assert isinstance(decision, governance.Defer)
    assert decision.until is not None
    local = decision.until.astimezone(IST)
    assert local.date() == at.astimezone(IST).date()
    assert (local.hour, local.minute) >= (8, 0) and local < datetime(2026, 10, 8, 8, 10, tzinfo=IST)
    # SMS at 22:30 IST -> next day 09:00-09:10
    late = datetime(2026, 10, 8, 22, 30, tzinfo=IST).astimezone(UTC)
    nxt = governance.next_window(
        late, IST, mkt_settings.parse_hhmm("09:00"), mkt_settings.parse_hhmm("21:00"), "seed"
    )
    assert nxt is not None
    assert (
        datetime(2026, 10, 9, 9, 0, tzinfo=IST)
        <= nxt.astimezone(IST)
        < datetime(2026, 10, 9, 9, 10, tzinfo=IST)
    )
    inside = datetime(2026, 10, 8, 12, 0, tzinfo=IST).astimezone(UTC)
    assert isinstance(await _check(tid, cid, "email", inside), governance.Send)


async def test_tenant_cannot_raise_the_cap_or_widen_windows(dorg: DOrg) -> None:
    m = dorg.admin
    for body in (
        {"frequency_cap_count": 4},
        {"send_windows": {"email": {"start": "07:00", "end": "21:00"}}},
        {"send_windows": {"sms": {"start": "09:00", "end": "22:00"}}},
        {"send_windows": {"email": {"start": "12:00", "end": "10:00"}}},
        {"rate_per_minute": {"email": 999999}},
    ):
        resp = await m.client.patch(f"{API}/settings", json=body, headers=h(m))
        assert resp.status_code == 422, (body, resp.text)
    ok = await m.client.patch(
        f"{API}/settings",
        json={"frequency_cap_count": 2, "send_windows": {"sms": {"start": "10:00", "end": "20:00"}}},
        headers=h(m),
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["frequency_cap_count"] == 2 and body["send_windows"]["sms"] == {
        "start": "10:00",
        "end": "20:00",
    }
    assert body["send_windows"]["email"] == {"start": "08:00", "end": "21:00"}


async def test_token_bucket_and_daily_cap() -> None:
    tid = uuid.uuid4()
    results = [await throttle.take(tid, "email", rate=3, daily_cap=100) for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert results[3].retry_after_ms > 0
    other = uuid.uuid4()
    capped = [await throttle.take(other, "sms", rate=100, daily_cap=2) for _ in range(3)]
    assert [r.allowed for r in capped] == [True, True, False] and capped[2].daily_exhausted
