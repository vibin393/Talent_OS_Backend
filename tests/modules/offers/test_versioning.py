"""FR-44: revisions of approved / released offers, locks and optimistic concurrency."""

from app.core import secure_links
from app.core.database import tenant_session
from tests.modules.demands.helpers import DOrg, h
from tests.modules.offers.helpers import (
    OFFERS,
    create_offer,
    emails,
    get_offer,
    link_token,
    patch,
    released_offer,
    setup_selected,
    submit,
)


async def _links(org: DOrg, offer_id: str) -> list[secure_links.SecureLink]:
    import uuid

    async with tenant_session(org.tenant_id) as db:
        return await secure_links.links_for_subject(db, "offer", uuid.UUID(offer_id))


async def test_pay_change_on_released_creates_draft_v2_and_revokes_links(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    assert any(link.revoked_at is None for link in await _links(dorg, offer["id"]))
    resp = await patch(dorg.admin, offer, pay_rate={"amount": "1200.00", "currency": "INR"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["current_version"]["version_no"] == 2
    assert (
        body["current_version"]["approval_state"] == "pending" and body["current_version"]["frozen"] is False
    )
    assert body["released_version_no"] == 1  # the released version stays readable
    v1 = await dorg.admin.client.get(f"{OFFERS}/{offer['id']}/versions/1")
    assert v1.json()["pay_rate"]["amount"] == "1100.00"
    assert all(link.revoked_at is not None for link in await _links(dorg, offer["id"]))
    # Must be re-approved (auto here) before release.
    again = await submit(dorg.admin, offer["id"])
    assert again["status"] == "approved"


async def test_pay_change_on_approved_requires_reapproval(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await submit(dorg.admin, (await create_offer(dorg.admin, s.application_id))["id"])
    resp = await patch(
        dorg.admin, offer, start_date=offer["current_version"]["start_date"], employment_type="permanent"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft" and resp.json()["current_version"]["version_no"] == 2


async def test_benefits_change_on_released_keeps_release_and_reemails(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    before = len(emails(dorg.tenant_id, "offer_updated"))
    resp = await patch(dorg.admin, offer, benefits_summary="Health cover and 24 days leave")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "released"
    assert body["current_version"]["version_no"] == 2
    assert body["current_version"]["approval_state"] == "not_required"
    assert body["released_version_no"] == 2
    updated = emails(dorg.tenant_id, "offer_updated")
    assert len(updated) == before + 1
    assert "l/" in updated[-1].text and link_token(updated[-1])
    fresh = await get_offer(dorg.admin, offer["id"])
    assert fresh["current_version"]["letter_ready"] is True


async def test_pending_approval_edit_is_409_and_stale_lock_409(dorg: DOrg) -> None:
    from tests.modules.approvals.test_approvals_api import make_chain

    await make_chain(
        dorg, [{"name": "Admin", "approver_user_id": str(dorg.admin.user_id)}], subject_type="offer"
    )
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    stale = await patch(dorg.admin, {**offer, "lock_version": offer["lock_version"] + 5}, title="New title")
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "version_conflict")
    pending = await submit(dorg.admin, offer["id"])
    assert pending["status"] == "pending_approval"
    locked = await patch(dorg.admin, pending, title="Other")
    assert (locked.status_code, locked.json()["error"]["code"]) == (409, "offer_locked_pending_approval")
    # Withdraw cancels the pending request.
    wd = await dorg.admin.client.post(
        f"{OFFERS}/{offer['id']}/withdraw", json={"reason": "Candidate pulled out"}, headers=h(dorg.admin)
    )
    assert wd.status_code == 200 and wd.json()["status"] == "withdrawn"
    assert wd.json()["approvals"][0]["status"] == "cancelled"
