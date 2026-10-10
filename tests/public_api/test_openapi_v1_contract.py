# ruff: noqa: E501
"""Epic 67 (story 67.6, FR-347): the generated public document matches the pin ``openapi-public-v1.json``; breaking
changes are detected; every addition must be in ``public_api_changelog.yaml``; every public route declares a scope."""

import copy

import yaml
from fastapi import FastAPI

from app.core import public_api_contract as contract
from app.core.public_api import is_public_route

OPEN_ROUTES = {"/api/public/v1/meta", "/api/public/v1/openapi.json"}  # 66: authenticated, no resource scope
# Epic 68 (story 68.2): any public principal; the event list is filtered to the types the caller's scopes allow.
OPEN_ROUTES |= {"/api/public/v1/event_types", "/api/public/v1/events", "/api/public/v1/events/{event_id}"}


def test_document_matches_the_pin(app: FastAPI) -> None:
    current = contract.contract_document(app)
    pinned = contract.load_pin()
    assert contract.breaking_changes(pinned, current) == []
    paths, fields = contract.additions(pinned, current)
    entries = yaml.safe_load(contract.CHANGELOG.read_text(encoding="utf-8"))
    assert contract.changelog_covers(entries, paths, fields) == []
    for entry in entries:
        assert entry["kind"] in ("added", "deprecated", "fixed") and entry["version"] == "v1"


def test_breaking_change_detection(app: FastAPI) -> None:
    base = contract.contract_document(app)
    path = "/api/public/v1/candidates"
    removed = copy.deepcopy(base)
    del removed["paths"][path]
    assert any("removed path" in p for p in contract.breaking_changes(base, removed))
    retyped = copy.deepcopy(base)
    retyped["components"]["schemas"]["CandidateOut"]["properties"]["full_name"] = {"type": "integer"}
    assert any("retyped" in p for p in contract.breaking_changes(base, retyped))
    dropped = copy.deepcopy(base)
    del dropped["components"]["schemas"]["CandidateOut"]["properties"]["current_title"]
    assert any("removed" in p for p in contract.breaking_changes(base, dropped))
    scoped = copy.deepcopy(base)
    scoped["paths"][path]["get"]["x-required-scopes"].append("pii.contact:read")
    assert any("added required scope" in p for p in contract.breaking_changes(base, scoped))
    required = copy.deepcopy(base)
    required["components"]["schemas"]["DemandCreateIn"].setdefault("required", []).append("positions")
    assert any("required request field" in p for p in contract.breaking_changes(base, required))
    narrowed = copy.deepcopy(base)
    narrowed["components"]["schemas"]["ApplicationCreateIn"]["enum"] = ["a"]
    base_enum = copy.deepcopy(base)
    base_enum["components"]["schemas"]["ApplicationCreateIn"]["enum"] = ["a", "b"]
    assert any("enum narrowed" in p for p in contract.breaking_changes(base_enum, narrowed))
    added = copy.deepcopy(base)
    added["paths"]["/api/public/v1/new_thing"] = {"get": {}}
    paths, _ = contract.additions(base, added)
    assert paths == ["/api/public/v1/new_thing"]
    assert contract.changelog_covers([{"paths": [], "fields": []}], paths, []) == paths


def test_every_public_route_declares_a_scope(app: FastAPI) -> None:
    doc = contract.contract_document(app)
    for route in app.routes:
        if not is_public_route(route) or route.path in OPEN_ROUTES:
            continue
        for method in route.methods:
            assert doc["paths"][route.path][method.lower()]["x-required-scopes"], (method, route.path)


async def test_changelog_endpoint(app: FastAPI) -> None:
    from tests.modules.identity.conftest import new_client

    async with new_client(app) as c:
        resp = await c.get("/api/public/v1/changelog")
    assert resp.status_code == 200 and resp.json()["entries"][0]["version"] == "v1"
    assert "max-age=3600" in resp.headers["cache-control"]
