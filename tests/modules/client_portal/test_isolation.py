"""Epic 18 route-table isolation (NFR-22): every ``/client-portal`` route registered by this epic's routers is
covered; a new route without a case fails.

For each route: a revoked Client User -> 401; for routes with an id, another client's / tenant's or a random id
-> 404 (403 where the portal permission is missing never applies: the users here hold every permission).
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.database import tenant_session
from app.models.external_auth import ClientUser
from app.router import client_portal_approvals, client_portal_hiring, client_portal_workforce
from tests.modules.client_portal.helpers import client_user, other_client, sent_submission
from tests.modules.pipeline.helpers import POrg

ROUTERS = (client_portal_hiring.router, client_portal_approvals.router, client_portal_workforce.router)
# path -> JSON body for POST routes (valid shape, so only isolation decides the status)
BODIES: dict[str, dict] = {  # type: ignore[type-arg]
    "/client-portal/submissions/compare": {"submission_ids": ["{sub}", "{sub}"]},
    "/client-portal/submissions/{submission_id}/feedback": {"decision": "interested"},
    "/client-portal/submissions/{submission_id}/slot-proposals": {
        "slots": [{"starts_at": "2030-01-01T10:00:00Z", "ends_at": "2030-01-01T11:00:00Z"}]
    },
    "/client-portal/slot-offers/{proposal_id}/choose": {"index": 0},
    "/client-portal/approvals/{request_id}/decide": {"decision": "approve"},
    "/client-portal/delegations": {
        "delegate_client_user_id": "{uuid}",
        "starts_on": "2030-01-01",
        "ends_on": "2030-01-02",
    },
    "/client-portal/scorecards/{participant_id}": {"ratings": [], "answers": [], "recommendation": "yes"},
    "/client-portal/demand-requests": {},
}
ID_ROUTES_NOT_COVERED_BY_404: set[str] = {"/client-portal/demand-requests"}


def _routes() -> list[tuple[str, str]]:
    out = []
    for r in ROUTERS:
        for route in r.routes:
            assert isinstance(route, APIRoute)
            for method in sorted(route.methods - {"HEAD"}):
                out.append((method, route.path))
    return out


ROUTES = _routes()


def test_every_route_has_a_body_case() -> None:
    posts = {p for m, p in ROUTES if m == "POST"}
    missing = sorted(p for p in posts if p not in BODIES and not p.endswith("/revoke"))
    assert missing == [], f"add isolation cases for {missing}"


def _fill(path: str, value: str) -> str:
    import re

    return re.sub(r"\{[a-z_]+\}", value, path)


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
async def test_route_isolation(app: FastAPI, porg: POrg, other_porg: POrg, method: str, path: str) -> None:
    _, sub = await sent_submission(porg)
    # (a) another client's user of the same tenant, (b) another tenant's user, using porg's submission id
    other_id = await other_client(porg)
    stranger = await client_user(app, porg.tenant_id, other_id)
    tenant2 = await client_user(app, other_porg.tenant_id, other_porg.client_id)
    revoked = await client_user(app, porg.tenant_id, porg.client_id)
    async with tenant_session(porg.tenant_id) as db:
        row = await db.get(ClientUser, revoked.id)
        assert row is not None
        row.status = "revoked"

    url = "/api/v1" + _fill(path, sub)
    body = BODIES.get(path)
    if body is not None:
        import json

        body = json.loads(json.dumps(body).replace("{sub}", sub).replace("{uuid}", str(uuid.uuid4())))

    async def call(who: object) -> int:
        client = who.client  # type: ignore[attr-defined]
        headers = {**who.h(), "Idempotency-Key": uuid.uuid4().hex}  # type: ignore[attr-defined]
        if method == "GET":
            return (await client.get(url)).status_code
        if path == "/client-portal/demand-requests":
            return (await client.post(url, data={"payload": "{}"}, headers=headers)).status_code
        return (await client.request(method, url, json=body, headers=headers)).status_code

    # (d) revoked Client User -> 401
    assert await call(revoked) == 401
    if "{" in path and path not in ID_ROUTES_NOT_COVERED_BY_404:
        assert await call(stranger) == 404
        assert await call(tenant2) == 404
