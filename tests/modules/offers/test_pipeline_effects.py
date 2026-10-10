"""Offer status -> pipeline stage and demand counts / Filled (§5.2 rows, FR-45, FR-15)."""

import uuid
from typing import Any
from unittest.mock import patch as mock_patch

from app.core.database import tenant_session
from app.services import offers_service
from tests.modules.demands.helpers import DEMANDS, DOrg, h
from tests.modules.offers.helpers import (
    OFFERS,
    create_offer,
    released_offer,
    setup_selected,
    stage_key,
    submit,
)


async def _accept(s: object, offer_id: str) -> None:
    async with tenant_session(s.org.tenant_id) as db:  # type: ignore[attr-defined]
        await offers_service.CandidateResponder(db, s.candidate_id, "portal", "203.0.113.4", "pytest").accept(  # type: ignore[attr-defined]
            uuid.UUID(offer_id), "Asha Rao", 1, True
        )


async def test_release_offered_then_accept_hired_and_filled(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    assert await stage_key(dorg, s.application_id) == "offered"
    demand = (await dorg.admin.client.get(f"{DEMANDS}/{s.demand_id}")).json()
    assert demand["offers_released_count"] == 1
    from app.services import demands as demands_service

    calls: list[int] = []
    original = demands_service.apply_positions_filled

    async def spy(ctx: Any, db: Any, demand_id: Any, filled: int) -> Any:
        calls.append(filled)
        return await original(ctx, db, demand_id, filled)

    with mock_patch.object(demands_service, "apply_positions_filled", spy):
        await _accept(s, offer["id"])
    assert calls == [1]
    assert await stage_key(dorg, s.application_id) == "hired"
    demand = (await dorg.admin.client.get(f"{DEMANDS}/{s.demand_id}")).json()
    assert demand["status"] == "filled" and demand["positions_filled"] == 1


async def test_release_refused_when_demand_not_open(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    await submit(dorg.admin, offer["id"])
    closed = await dorg.admin.client.post(
        f"{DEMANDS}/{s.demand_id}/transition",
        json={"to": "closed", "reason_code": "client_closed", "comment": "done"},
        headers=h(dorg.admin),
    )
    assert closed.status_code == 200, closed.text
    resp = await dorg.admin.client.post(f"{OFFERS}/{offer['id']}/release", headers=h(dorg.admin))
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "demand_not_open")


async def test_withdraw_after_release_back_to_selected(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    resp = await dorg.admin.client.post(
        f"{OFFERS}/{offer['id']}/withdraw", json={"reason": "Client froze the role"}, headers=h(dorg.admin)
    )
    assert resp.status_code == 200, resp.text
    assert await stage_key(dorg, s.application_id) == "selected"
    demand = (await dorg.admin.client.get(f"{DEMANDS}/{s.demand_id}")).json()
    assert demand["offers_released_count"] == 0


async def test_decline_back_to_selected(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await released_offer(s)
    async with tenant_session(dorg.tenant_id) as db:
        await offers_service.CandidateResponder(db, s.candidate_id, "portal", None, None).decline(
            uuid.UUID(offer["id"]), None, 1
        )
    assert await stage_key(dorg, s.application_id) == "selected"
