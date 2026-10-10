"""Candidate Portal offer API: own offers only (other -> 404), released-or-later only, no bill rate / margin."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from app.dependencies.candidate import current_candidate
from tests.modules.demands.helpers import DOrg
from tests.modules.identity.conftest import new_client
from tests.modules.offers.helpers import create_offer, get_offer, released_offer, setup_selected

PORTAL = "/api/v1/portal/offers"


@pytest.fixture
def as_candidate(app: FastAPI) -> Iterator[Any]:
    def _set(tenant_id: Any, candidate_id: Any) -> None:
        principal = SimpleNamespace(tenant_id=tenant_id, candidate_id=candidate_id, id=candidate_id)
        app.dependency_overrides[current_candidate] = lambda: principal

    yield _set
    app.dependency_overrides.pop(current_candidate, None)


async def test_unauthenticated_is_401(app: FastAPI) -> None:
    async with new_client(app) as anon:
        assert (await anon.get(PORTAL)).status_code == 401


async def test_candidate_sees_only_own_released_offers(
    app: FastAPI, dorg: DOrg, other_dorg: DOrg, as_candidate: Any
) -> None:
    mine = await setup_selected(dorg)
    offer = await released_offer(mine)
    other = await setup_selected(dorg, name="Ravi Kumar")
    draft = await create_offer(dorg.admin, other.application_id)
    foreign = await setup_selected(other_dorg)
    foreign_offer = await released_offer(foreign)

    as_candidate(dorg.tenant_id, mine.candidate_id)
    async with new_client(app) as c:
        listed = (await c.get(PORTAL)).json()["items"]
        assert [o["id"] for o in listed] == [offer["id"]]
        assert "bill_rate" not in listed[0] and "margin_pct" not in listed[0]
        assert listed[0]["letter_url"] == f"{PORTAL}/{offer['id']}/letter"
        assert (await c.get(f"{PORTAL}/{draft['id']}")).status_code == 404
        assert (await c.get(f"{PORTAL}/{foreign_offer['id']}")).status_code == 404
        got = await c.get(f"{PORTAL}/{offer['id']}")
        assert got.status_code == 200 and got.json()["status"] == "released"
        letter = await c.get(f"{PORTAL}/{offer['id']}/letter")
        assert letter.status_code == 200 and letter.headers["content-type"] == "application/pdf"
        assert (await get_offer(dorg.admin, offer["id"]))["status"] == "released"
        viewed = await c.post(f"{PORTAL}/{offer['id']}/viewed")
        assert viewed.status_code == 200 and viewed.json()["status"] == "viewed"
        foreign_accept = await c.post(
            f"{PORTAL}/{foreign_offer['id']}/accept",
            json={"typed_name": "Asha Rao", "version_no": 1, "agreed": True},
        )
        assert foreign_accept.status_code == 404
        ok = await c.post(
            f"{PORTAL}/{offer['id']}/accept", json={"typed_name": "Asha Rao", "version_no": 1, "agreed": True}
        )
        assert ok.status_code == 200, ok.text
    detail = await get_offer(dorg.admin, offer["id"])
    assert detail["status"] == "accepted" and detail["responses"][0]["channel"] == "portal"

    as_candidate(dorg.tenant_id, other.candidate_id)
    async with new_client(app) as c:
        assert (await c.get(f"{PORTAL}/{offer['id']}")).status_code == 404
        assert (await c.post(f"{PORTAL}/{offer['id']}/decline", json={"version_no": 1})).status_code == 404
