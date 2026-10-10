"""Rate segments (overlap / order / immutability / approval / contexts) and pre-joining (touchpoints, link)."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.placements import PlacementTouchpoint
from app.services import placements_service
from app.services.placements import prejoining
from tests.modules.demands.helpers import DOrg, admin_ctx, h
from tests.modules.offers.helpers import emails, link_token
from tests.modules.placements.helpers import PLACEMENTS, direct_placement, set_start

D = date.today()


def _iso(d: date) -> str:
    return d.isoformat()


async def test_segments_close_previous_refuse_out_of_order_and_contexts(dorg: DOrg) -> None:
    body = await direct_placement(dorg, planned_start_date=_iso(D - timedelta(days=60)))
    pid = body["id"]
    url = f"{PLACEMENTS}/{pid}/rate-segments"
    resp = await dorg.admin.client.post(
        url,
        json={
            "effective_from": _iso(D - timedelta(days=10)),
            "pay_rate": "1100",
            "bill_rate": "1700",
            "reason": "Raise",
        },
        headers=h(dorg.admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "active" and resp.json()["margin_amount"] == "600.00"
    segs = (await dorg.admin.client.get(url)).json()
    assert [s["effective_to"] for s in segs] == [_iso(D - timedelta(days=11)), None]
    old = await dorg.admin.client.post(
        url,
        json={"effective_from": _iso(D - timedelta(days=30)), "pay_rate": "1", "reason": "Backdated"},
        headers=h(dorg.admin),
    )
    assert old.status_code == 409 and old.json()["error"]["code"] == "segment_out_of_order"
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        early = await placements_service.billing_context(ctx, db, uuid.UUID(pid), D - timedelta(days=20))
        late = await placements_service.billing_context(ctx, db, uuid.UUID(pid), D)
        pay = await placements_service.pay_context(ctx, db, uuid.UUID(pid), D)
        assert early.bill_rate == Decimal("1500") and late.bill_rate == Decimal("1700")
        assert isinstance(pay.pay_rate, Decimal) and pay.pay_rate == Decimal("1100")
        with pytest.raises(NotFound):
            await placements_service.billing_context(ctx, db, uuid.UUID(pid), D - timedelta(days=365))
        seg = await placements_service.rate_segment_on(ctx, db, uuid.UUID(pid), D)
        assert seg is not None and (await placements_service.get_rate_segment(ctx, db, seg.id)).id == seg.id
        summary = await placements_service.get_placement_summary(ctx, db, uuid.UUID(pid))
        assert summary.human_id == body["human_id"]
    # the append-only trigger refuses a rate UPDATE
    with pytest.raises(DBAPIError):
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(
                text("UPDATE rate_segments SET bill_rate = 1 WHERE placement_id = :p"), {"p": uuid.UUID(pid)}
            )


async def test_low_margin_segment_needs_approval(dorg: DOrg) -> None:
    await dorg.admin.client.patch(
        f"{PLACEMENTS}/settings", json={"min_margin_pct": "20"}, headers=h(dorg.admin)
    )
    body = await direct_placement(dorg, planned_start_date=_iso(D - timedelta(days=30)))
    url = f"{PLACEMENTS}/{body['id']}/rate-segments"
    resp = await dorg.admin.client.post(
        url,
        json={"effective_from": _iso(D), "pay_rate": "1000", "bill_rate": "1050", "reason": "Squeeze"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 201, resp.text
    seg = resp.json()
    assert seg["status"] == "pending_approval" and seg["approval_request_id"]
    decided = await dorg.admin.client.post(
        f"/api/v1/approvals/{seg['approval_request_id']}/decide",
        json={"action": "approve"},
        headers=h(dorg.admin),
    )
    assert decided.status_code == 200, decided.text
    segs = (await dorg.admin.client.get(url)).json()
    assert [s["status"] for s in segs] == ["active", "active"]
    assert segs[0]["effective_to"] == _iso(D - timedelta(days=1))


def test_rates_masked_without_compensation_view() -> None:
    from app.core.permissions import CATALOG, Principal, Scope
    from app.router.placements import _masked

    perms = {k: Scope.TENANT for k in CATALOG if k != "compensation:view"}
    viewer = Principal(type="user", id=uuid.uuid4(), tenant_id=uuid.uuid4(), permissions=perms)
    row = {
        "pay_rate": "1",
        "bill_rate": "2",
        "vendor_rate": "3",
        "margin_amount": "1",
        "margin_pct": "50",
        "burden_pct": "1",
        "status": "active",
    }
    out = _masked([row], viewer)[0]
    assert all(
        out[k] is None
        for k in ("pay_rate", "bill_rate", "vendor_rate", "margin_amount", "margin_pct", "burden_pct")
    )
    assert out["status"] == "active"


async def test_touchpoints_idempotent_and_link_consumed_once(dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    pid = uuid.UUID(body["id"])
    await set_start(dorg, pid, D + timedelta(days=7))
    ctx = admin_ctx(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        first = await prejoining.run_daily(ctx, db)
    async with tenant_session(dorg.tenant_id) as db:
        second = await prejoining.run_daily(ctx, db)
    assert first.touchpoints == 1 and second.touchpoints == 0
    msg = first.emails[0]
    assert msg.kind == "placement_joining_confirmation" and "/l/" in msg.text
    from app.core.mailer import get_mailer

    get_mailer().send(msg)
    token = link_token(emails(dorg.tenant_id, "placement_joining_confirmation")[-1])
    # GET never mutates
    for _ in range(2):
        view = await dorg.admin.client.get(f"/api/v1/public/links/{token}")
        assert view.status_code == 200, view.text
        assert (
            view.json()["purpose"] == "joining_confirmation" and view.json()["joining"]["responded"] is False
        )
    resp = await dorg.admin.client.post(
        f"/api/v1/public/links/{token}/joining", json={"response": "confirmed"}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "confirmed"
    again = await dorg.admin.client.post(
        f"/api/v1/public/links/{token}/joining", json={"response": "confirmed"}
    )
    assert again.status_code == 410
    detail = (await dorg.admin.client.get(f"{PLACEMENTS}/{pid}")).json()
    assert detail["status"] == "joining_confirmed" and detail["joining_confirmed_via"] == "secure_link"
    async with tenant_session(dorg.tenant_id) as db:
        tp = (
            await db.execute(select(PlacementTouchpoint).where(PlacementTouchpoint.placement_id == pid))
        ).scalar_one()
        assert tp.response == "confirmed" and tp.offset_days == 7


async def test_counter_offer_raises_dropout_and_clear(dorg: DOrg) -> None:
    body = await direct_placement(dorg)
    pid = body["id"]
    resp = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/counter-offer", json={"note": "Got 20% more"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dropout_risk"] is True and "counter_offer" in resp.json()["dropout_risk_rules"]
    cleared = await dorg.admin.client.post(
        f"{PLACEMENTS}/{pid}/dropout-risk/clear", json={}, headers=h(dorg.admin)
    )
    assert cleared.json()["dropout_risk"] is False
    tracker = (await dorg.admin.client.get(f"{PLACEMENTS}/pre-joining")).json()
    assert any(r["placement_id"] == pid for r in tracker)


async def test_retro_segments_activate_deterministically(dorg: DOrg) -> None:
    """Regression (Epic 25 report): the previous segment is end-dated in its own flush before the new one
    activates, so the non-deferrable EXCLUDE never sees a transient overlap. 20 retro additions in a row."""
    body = await direct_placement(dorg, planned_start_date=_iso(D - timedelta(days=60)))
    url = f"{PLACEMENTS}/{body['id']}/rate-segments"
    for i in range(1, 21):
        resp = await dorg.admin.client.post(
            url,
            json={"effective_from": _iso(D - timedelta(days=60 - i)), "pay_rate": str(1000 + i), "bill_rate": "1600", "reason": f"retro {i}"},
            headers=h(dorg.admin),
        )
        assert resp.status_code == 201, (i, resp.text)
        assert resp.json()["status"] == "active"
    segs = (await dorg.admin.client.get(url)).json()
    assert len(segs) == 21
    assert [s["effective_to"] for s in segs[:-1]] == [_iso(D - timedelta(days=60 - i)) for i in range(0, 20)]
    assert segs[-1]["effective_to"] is None
