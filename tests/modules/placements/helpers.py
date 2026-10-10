"""Shared helpers for the Epic 19 placement tests."""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.placements import Placement
from app.services import offers_service
from tests.modules.demands.helpers import DOrg, h
from tests.modules.offers.helpers import Setup, released_offer, setup_selected

PLACEMENTS = "/api/v1/placements"
WORKERS = "/api/v1/workers"


async def accept(s: Setup, offer_id: str) -> None:
    async with tenant_session(s.org.tenant_id) as db:
        await offers_service.CandidateResponder(db, s.candidate_id, "portal", "203.0.113.4", "pytest").accept(
            uuid.UUID(offer_id), "Asha Rao", 1, True
        )


async def placement_for_offer(org: DOrg, offer_id: str | uuid.UUID) -> Placement | None:
    async with tenant_session(org.tenant_id) as db:
        return (
            await db.execute(select(Placement).where(Placement.offer_id == uuid.UUID(str(offer_id))))
        ).scalar_one_or_none()


async def accepted_placement(org: DOrg, **offer_body: Any) -> tuple[Setup, dict[str, Any], Placement]:
    s = await setup_selected(org)
    offer = await released_offer(s, **offer_body)
    await accept(s, offer["id"])
    p = await placement_for_offer(org, offer["id"])
    assert p is not None, "OfferAccepted must create a placement"
    return s, offer, p


async def direct_placement(org: DOrg, candidate_id: uuid.UUID | None = None, **body: Any) -> dict[str, Any]:
    from tests.modules.offers.helpers import create_candidate

    cand = candidate_id or await create_candidate(org.admin, f"Wren Worker {uuid.uuid4().hex[:4]}")
    payload: dict[str, Any] = {
        "candidate_id": str(cand),
        "client_id": str(org.client_id),
        "planned_start_date": (date.today() - timedelta(days=1)).isoformat(),
        "rate_unit": "hour",
        "pay_rate": "1000.00",
        "bill_rate": "1500.00",
        "burden_pct": "10",
        "role_title": "Data Engineer",
    }
    payload.update(body)
    resp = await org.admin.client.post(PLACEMENTS, json=payload, headers=h(org.admin))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def waive_all(org: DOrg, placement_id: str) -> None:
    """Request an override of every unmet gate (no approval chain -> auto-approved -> waived)."""
    gates = (await org.admin.client.get(f"{PLACEMENTS}/{placement_id}/start-gates")).json()
    ids = [g["id"] for g in gates if g["required"] and g["status"] not in ("met", "waived", "not_required")]
    if not ids:
        return
    resp = await org.admin.client.post(
        f"{PLACEMENTS}/{placement_id}/gate-overrides",
        json={"gate_ids": ids, "reason": "Client confirmed in writing"},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text


async def set_start(org: DOrg, placement_id: str | uuid.UUID, start: date) -> None:
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE placements SET planned_start_date = :d WHERE id = :i"),
            {"d": start, "i": uuid.UUID(str(placement_id))},
        )
