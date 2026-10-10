"""Offer expiry (AD-9, AD-14): end of the expiry day in the tenant timezone; beat task idempotent; owner emailed."""

from datetime import UTC, date, datetime, timedelta

from freezegun import freeze_time

from app.core.database import tenant_session
from app.services.offers.common import end_of_day_utc
from app.tasks import offers as offer_tasks
from tests.modules.demands.helpers import DOrg
from tests.modules.offers.helpers import (
    emails,
    get_offer,
    released_offer,
    set_expires,
    setup_selected,
    stage_key,
    utcnow,
)


def test_end_of_day_in_tenant_timezone() -> None:
    day = date(2026, 10, 15)
    # Asia/Kolkata is UTC+05:30: local midnight after 15 Oct = 15 Oct 18:30 UTC.
    assert end_of_day_utc(day, "Asia/Kolkata") == datetime(2026, 10, 15, 18, 30, tzinfo=UTC)
    assert end_of_day_utc(day, "UTC") == datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
    with freeze_time("2026-10-15T18:29:00Z"):
        assert datetime.now(UTC) < end_of_day_utc(day, "Asia/Kolkata")
    with freeze_time("2026-10-15T18:31:00Z"):
        assert datetime.now(UTC) >= end_of_day_utc(day, "Asia/Kolkata")


async def test_release_sets_expires_at_from_tenant_timezone(dorg: DOrg) -> None:
    from app.services import identity_bridge

    s = await setup_selected(dorg)
    offer = await released_offer(s)
    async with tenant_session(dorg.tenant_id) as db:
        tenant = await identity_bridge.get_tenant(db, dorg.tenant_id)
        tz = tenant.timezone if tenant else "UTC"
    expiry = date.fromisoformat(offer["current_version"]["expiry_date"])
    assert datetime.fromisoformat(offer["expires_at"].replace("Z", "+00:00")) == end_of_day_utc(expiry, tz)


async def test_beat_task_expires_idempotently_and_notifies_owner(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    await set_expires(dorg, offer["id"], utcnow() - timedelta(minutes=5))
    assert offer_tasks.expire_due_tenant(str(dorg.tenant_id)) == 1
    assert offer_tasks.expire_due_tenant(str(dorg.tenant_id)) == 0
    detail = await get_offer(dorg.admin, offer["id"])
    assert detail["status"] == "expired"
    assert detail["history"][-1]["actor_kind"] == "system"
    assert await stage_key(dorg, s.application_id) == "selected"
    assert len(emails(dorg.tenant_id, "offer_expired_owner")) == 1


async def test_expiry_warning_once(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    await set_expires(dorg, offer["id"], utcnow() + timedelta(hours=10))
    assert offer_tasks.expiry_warnings_tenant(str(dorg.tenant_id)) == 1
    assert offer_tasks.expiry_warnings_tenant(str(dorg.tenant_id)) == 0
    assert len(emails(dorg.tenant_id, "offer_expiry_warning")) == 2  # owner + candidate
