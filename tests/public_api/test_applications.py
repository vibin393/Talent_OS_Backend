# ruff: noqa: E501, F811
"""Epic 67 (story 67.5): ``/public/v1/applications`` -- create, list / get, non-★ stage moves, ★ refusal
(``human_only_action``) for every human-only stage, and no ★ route in the public OpenAPI."""

from typing import Any

import pytest
from fastapi import FastAPI

from app.services.public_api.applications import star_stages
from tests.modules.pipeline.helpers import API, POrg, create_candidate, h, porg  # noqa: F401
from tests.public_api.e67 import Api, app_token, code, installed, user_token


async def _custom_stage(m: Any) -> str:
    resp = await m.client.post(f"{API}/pipeline/stages", json={"label": "Phone screen", "key": "phone_screen"}, headers=h(m))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["key"])


async def test_create_and_non_star_moves(app: FastAPI, porg: POrg) -> None:
    inst = await installed(porg)  # type: ignore[arg-type]
    api = Api(app, await user_token(inst, porg.admin.user_id))
    cand = await create_candidate(porg.admin, "Kiran Das")
    resp = await api.post("/applications", {"candidate_id": cand["id"], "demand_id": porg.demand_id})
    assert resp.status_code == 201, resp.text
    application = resp.json()
    assert application["stage"] == "applied" and application["source"] == "public_api"
    dup = await api.post("/applications", {"candidate_id": cand["id"], "demand_id": porg.demand_id})
    assert (dup.status_code, code(dup)) == (409, "application_exists")
    key = await _custom_stage(porg.admin)
    moved = await api.post(f"/applications/{application['id']}/stage", {"to_stage": key})
    assert moved.status_code == 200, moved.text
    assert moved.json()["stage"] == key
    back = await api.post(f"/applications/{application['id']}/stage", {"to_stage": "applied"})
    assert back.status_code == 200, back.text
    # a non-★ but disallowed move passes the domain 409 through
    jump = await api.post(f"/applications/{application['id']}/stage", {"to_stage": "interviewing"})
    assert (jump.status_code, code(jump)) == (409, "transition_not_allowed")
    listed = (await api.get("/applications", demand_id=porg.demand_id)).json()
    assert [a["id"] for a in listed["data"]] == [application["id"]]


@pytest.mark.parametrize("target", sorted(star_stages()))
async def test_star_stage_refused(app: FastAPI, porg: POrg, target: str) -> None:
    inst = await installed(porg)  # type: ignore[arg-type]
    cand = await create_candidate(porg.admin, "Star Target")
    for token in (await app_token(inst), await user_token(inst, porg.admin.user_id)):
        api = Api(app, token)
        created = await api.post("/applications", {"candidate_id": cand["id"], "demand_id": porg.demand_id})
        app_id = created.json()["id"] if created.status_code == 201 else None
        if app_id is None:
            listed = (await api.get("/applications", candidate_id=cand["id"])).json()
            app_id = listed["data"][0]["id"]
        resp = await api.post(f"/applications/{app_id}/stage", {"to_stage": target})
        assert (resp.status_code, code(resp)) == (403, "human_only_action"), resp.text


def test_star_stages_cover_the_catalogue() -> None:
    assert {"rejected", "offered", "hired", "selected", "submitted"} <= star_stages()


async def test_no_star_route_in_public_openapi(app: FastAPI) -> None:
    from app.core.public_api import public_openapi

    doc = public_openapi(app)
    paths = " ".join(doc["paths"])
    for word in ("reject", "approve", "approval", "release", "settings", "roles", "privacy", "dsr", "installations", "hire"):
        assert word not in paths, word
