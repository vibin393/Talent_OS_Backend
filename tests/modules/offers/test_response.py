"""FR-45: candidate response through the secure link (and shared responder rules)."""

import asyncio
import uuid
from datetime import timedelta

from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.offers import OfferResponse
from app.services.offers import handlers as offer_handlers
from app.services.offers import letter_pdf
from app.tasks import runtime as task_runtime
from tests.factories import deferred_enqueues
from tests.modules.demands.helpers import DOrg
from tests.modules.identity.conftest import new_client
from tests.modules.offers.helpers import (
    emails,
    frontend,
    get_offer,
    link_token,
    released_offer,
    set_expires,
    setup_selected,
    utcnow,
)

LINKS = "/api/v1/public/links"


def _post_headers() -> dict[str, str]:
    return {"Origin": frontend(), "User-Agent": "pytest-browser/1.0"}


async def _token(org: DOrg) -> str:
    (message,) = emails(org.tenant_id, "offer_released")[-1:]
    return link_token(message)


async def test_get_records_nothing_then_viewed_once_then_accept(app: FastAPI, dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    token = await _token(dorg)
    released_mail = emails(dorg.tenant_id, "offer_released")[-1]
    assert f"/portal/offers/{offer['id']}" in released_mail.text
    assert "1500" not in released_mail.text and "margin" not in released_mail.text.lower()
    async with new_client(app) as anon:
        view = await anon.get(f"{LINKS}/{token}")
        assert view.status_code == 200, view.text
        body = view.json()
        assert body["purpose"] == "offer_response" and body["offer"]["status"] == "released"
        assert "bill_rate" not in body["offer"] and "margin_pct" not in body["offer"]
        assert view.headers["cache-control"] == "no-store"
        letter = await anon.get(f"{LINKS}/{token}/letter")
        assert letter.status_code == 200 and "Asha Rao" in letter_pdf.pdf_text(letter.content)
        assert (await get_offer(dorg.admin, offer["id"]))["status"] == "released"  # GETs record nothing

        no_origin = await anon.post(f"{LINKS}/{token}/viewed")
        assert no_origin.status_code == 403
        viewed = await anon.post(f"{LINKS}/{token}/viewed", headers=_post_headers())
        assert viewed.status_code == 200 and viewed.json()["status"] == "viewed"
        again = await anon.post(f"{LINKS}/{token}/viewed", headers=_post_headers())
        assert again.status_code == 200
        detail = await get_offer(dorg.admin, offer["id"])
        assert [x["to_status"] for x in detail["history"]].count("viewed") == 1

        wrong_name = await anon.post(
            f"{LINKS}/{token}/accept",
            json={"typed_name": "Someone Else", "version_no": 1, "agreed": True},
            headers=_post_headers(),
        )
        assert (wrong_name.status_code, wrong_name.json()["error"]["code"]) == (422, "name_mismatch")
        wrong_version = await anon.post(
            f"{LINKS}/{token}/accept",
            json={"typed_name": "asha  RAO", "version_no": 2, "agreed": True},
            headers=_post_headers(),
        )
        assert (wrong_version.status_code, wrong_version.json()["error"]["code"]) == (
            409,
            "offer_version_changed",
        )
        not_agreed = await anon.post(
            f"{LINKS}/{token}/accept",
            json={"typed_name": "Asha Rao", "version_no": 1, "agreed": False},
            headers=_post_headers(),
        )
        assert not_agreed.status_code == 422
        ok = await anon.post(
            f"{LINKS}/{token}/accept",
            json={"typed_name": "asha  RAO", "version_no": 1, "agreed": True},
            headers={**_post_headers(), "X-Forwarded-For": "198.51.100.7"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "accepted" and ok.json()["offer"]["response"]["decision"] == "accepted"
        used = await anon.get(f"{LINKS}/{token}")
        assert used.status_code == 410

    detail = await get_offer(dorg.admin, offer["id"])
    assert detail["status"] == "accepted"
    (resp,) = detail["responses"]
    assert resp["typed_name"] == "asha RAO" and resp["version_no"] == 1
    assert resp["channel"] == "secure_link" and resp["user_agent"] == "pytest-browser/1.0"
    assert resp["letter_sha256"] == detail["current_version"]["letter_sha256"]
    assert resp["ip"] is not None
    # Signed PDF with the acceptance certificate, emailed to candidate and owner.
    signed = emails(dorg.tenant_id, "offer_accepted")
    assert len(signed) == 2
    text = letter_pdf.pdf_text(signed[0].attachments[0].data)
    flat = "".join(text.split())
    assert "acceptance certificate" in text.lower() and "ashaRAO" in flat
    assert resp["letter_sha256"] in flat  # long fingerprint wraps inside its table cell
    assert detail["accepted_letter_url"]


async def test_parallel_accepts_one_success(app: FastAPI, dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    await released_offer(s)
    token = await _token(dorg)
    body = {"typed_name": "Asha Rao", "version_no": 1, "agreed": True}
    async with new_client(app) as a, new_client(app) as b:
        # Epic 19: OfferAccepted also enqueues placements.create_from_offer (via tasks.runtime.enqueue).
        with deferred_enqueues(offer_handlers, task_runtime):
            results = await asyncio.gather(
                a.post(f"{LINKS}/{token}/accept", json=body, headers=_post_headers()),
                b.post(f"{LINKS}/{token}/accept", json=body, headers=_post_headers()),
            )
    codes = sorted(r.status_code for r in results)
    assert codes[0] == 200 and codes[1] in (409, 410), [r.text for r in results]
    async with tenant_session(dorg.tenant_id) as db:
        from sqlalchemy import func, select

        n = (await db.execute(select(func.count()).select_from(OfferResponse))).scalar_one()
    assert n == 1


async def test_accept_after_expiry_410_and_decline(app: FastAPI, dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    token = await _token(dorg)
    await set_expires(dorg, offer["id"], utcnow() - timedelta(minutes=1))
    async with new_client(app) as anon:
        late = await anon.post(
            f"{LINKS}/{token}/accept",
            json={"typed_name": "Asha Rao", "version_no": 1, "agreed": True},
            headers=_post_headers(),
        )
        assert (late.status_code, late.json()["error"]["code"]) == (410, "offer_expired")
    await set_expires(dorg, offer["id"], utcnow() + timedelta(days=2))
    async with new_client(app) as anon:
        declined = await anon.post(
            f"{LINKS}/{token}/decline",
            json={"reason": "Accepted another offer", "version_no": 1},
            headers=_post_headers(),
        )
        assert declined.status_code == 200, declined.text
    detail = await get_offer(dorg.admin, offer["id"])
    assert detail["status"] == "declined"
    assert detail["responses"][0]["decline_reason"] == "Accepted another offer"
    assert emails(dorg.tenant_id, "offer_declined_owner")


async def test_cross_tenant_link_and_bad_token(app: FastAPI, dorg: DOrg) -> None:
    async with new_client(app) as anon:
        resp = await anon.get(f"{LINKS}/{'x' * 43}")
        assert resp.status_code == 404
        resp = await anon.post(
            f"{LINKS}/{uuid.uuid4().hex}{uuid.uuid4().hex}/accept",
            json={"typed_name": "A", "version_no": 1, "agreed": True},
            headers=_post_headers(),
        )
        assert resp.status_code == 404
