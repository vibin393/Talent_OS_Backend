"""Story 5.6: header global search (FR-73)."""

from typing import Any

from fastapi import FastAPI

from app.services.search.global_search import classify
from tests.epic5 import GLOBAL, make_candidate
from tests.modules.demands.helpers import DOrg, create_demand, h
from tests.modules.identity.conftest import join


async def _global(m: Any, q: str, **params: Any) -> dict[str, Any]:
    resp = await m.client.get(GLOBAL, params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _group(result: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((g for g in result["groups"] if g["type"] == kind), None)


def test_classifier() -> None:
    assert classify("CAN-000123") == "human_id"
    assert classify("req-1056") == "human_id"
    assert classify("priya@example.com") == "email"
    assert classify("+91 98765 43210") == "phone"
    assert classify("98765") == "text"
    assert classify("Priya") == "text"


async def test_exact_ids_are_pinned_first(dorg: DOrg) -> None:
    m = dorg.admin
    cand = await make_candidate(
        m, "Priya Raman", emails=[{"email": "priya.raman@example.com"}], phones=[{"phone": "+91 98765 43210"}]
    )
    await make_candidate(m, "Priya Menon")
    demand = await create_demand(m, dorg.client_id, title="Priya Data Platform")
    res = await _global(m, cand["human_id"].lower())
    assert res["exact"]["id"] == cand["id"] and res["exact"]["type"] == "candidate"
    assert res["exact"]["url"] == f"/candidates/{cand['id']}"
    res = await _global(m, demand["req_id"])
    assert res["exact"]["id"] == demand["id"] and res["exact"]["type"] == "demand"
    res = await _global(m, "PRIYA.RAMAN@example.com")
    assert res["exact"]["id"] == cand["id"]
    res = await _global(m, "098765 43210")
    assert res["exact"]["id"] == cand["id"]
    res = await _global(m, "priya")
    assert res["exact"] is None
    titles = {i["title"] for i in _group(res, "candidate")["items"]}  # type: ignore[index]
    assert titles == {"Priya Raman", "Priya Menon"}
    assert [i["title"] for i in _group(res, "demand")["items"]] == ["Priya Data Platform"]  # type: ignore[index]
    # Phase 1 groups first, in order; Phase 2 registry groups (vendor, placement, invoice, ...) follow them.
    assert [g["type"] for g in res["groups"]][:4] == ["candidate", "demand", "client", "contact"]
    res = await _global(m, "priya", limit=1)
    assert _group(res, "candidate")["has_more"] is True  # type: ignore[index]


async def test_clients_and_contacts(dorg: DOrg) -> None:
    m = dorg.admin
    resp = await m.client.post("/api/v1/clients", json={"name": "Globex Banking"}, headers=h(m))
    client_id = resp.json()["id"]
    resp = await m.client.post(
        f"/api/v1/clients/{client_id}/contacts",
        json={"name": "Hank Scorpio", "email": "hank@globex.example"},
        headers=h(m),
    )
    assert resp.status_code == 201, resp.text
    res = await _global(m, "globex")
    assert [i["title"] for i in _group(res, "client")["items"]] == ["Globex Banking"]  # type: ignore[index]
    res = await _global(m, "hank@globex.example")
    assert res["exact"]["type"] == "contact"
    assert _group(res, "contact")["items"][0]["url"].startswith(f"/clients/{client_id}")  # type: ignore[index]


async def test_unpermitted_groups_are_absent(app: FastAPI, dorg: DOrg) -> None:
    interviewer = await join(app, dorg.tenant, "interviewer", name="Ivan Interviewer")
    res = await _global(interviewer, "priya")
    assert [g["type"] for g in res["groups"]] == ["candidate"]
    await interviewer.client.aclose()
    assert (await _global(dorg.admin, "p"))["groups"] == []


async def test_cross_tenant(dorg: DOrg, other_dorg: DOrg) -> None:
    cand = await make_candidate(dorg.admin, "Priya Raman")
    res = await _global(other_dorg.admin, cand["human_id"])
    assert res["exact"] is None
    assert all(not g["items"] for g in res["groups"])
