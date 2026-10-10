# ruff: noqa: E501
"""Epic 68 story 68.1: create (secret once, vault), verification handshake, Egress Guard at save, scope rule, limits,
human-only internal mutations, the public own-only surface and cross-tenant isolation."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.services import vault_service
from app.services.webhooks import common
from tests.epic3 import Org3
from tests.public_api.e67 import Api, code
from tests.support.oauth import install_app, make_app, mint
from tests.webhooks.conftest import Receiver, sub_row

BASE = "/api/v1/webhooks"


async def create(org: Org3, **body: Any) -> dict[str, Any]:
    payload = {"url": "https://hooks.example.com/in?token=abc", "event_types": ["placement.started", "timesheet.approved"], **body}
    resp = await org.admin.client.post(f"{BASE}/subscriptions", json=payload, headers=org.admin.h())
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def test_create_returns_secret_once_and_verifies(org3: Org3, receiver: Receiver) -> None:
    created = await create(org3, description="ATS sync")
    secret = created["signing_secret"]
    assert secret.startswith("whsec_")
    sub = created["subscription"]
    assert sub["signing_secret"]["fingerprint"]
    assert "whsec_" not in str(sub)
    row = await sub_row(org3.tenant_id, uuid.UUID(sub["id"]))
    assert row.status == "active", row.verification_error
    assert row.verified_at is not None
    sent = receiver.requests[0]
    assert sent.headers["TalentOS-Event-Type"] == "webhook.verification"
    assert "authorization" not in sent.headers
    assert "cookie" not in sent.headers
    got = await org3.admin.client.get(f"{BASE}/subscriptions/{sub['id']}")
    assert got.status_code == 200
    assert "whsec_" not in got.text
    async with tenant_session(org3.tenant_id) as db:
        value = await vault_service.get(
            RequestContext(tenant_id=org3.tenant_id, actor_kind="system"), db,
            vault_service.SecretRef("webhook_subscription", row.id, "signing_secret"), purpose="test",
        )  # fmt: skip
    assert value == secret


async def test_challenge_mismatch_stays_pending(org3: Org3, receiver: Receiver) -> None:
    receiver.body = b"nope"
    created = await create(org3)
    row = await sub_row(org3.tenant_id, uuid.UUID(created["subscription"]["id"]))
    assert row.status == "pending_verification"
    assert row.verification_error == "challenge_mismatch"
    receiver.body = None
    again = await org3.admin.client.post(f"{BASE}/subscriptions/{row.id}/verify", headers=org3.admin.h())
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "active"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://10.0.0.5/hook", "scheme"),
        ("https://169.254.169.254/", "ip_literal"),
        ("https://hooks.example.com:8080/x", "port"),
        ("https://user:pw@hooks.example.com/", "userinfo"),
        ("https://2130706433/", "ip_literal"),
        ("https://0x7f.1/", "ip_literal"),
        ("https://[::1]/", "ip_literal"),
    ],
)
async def test_egress_refused_at_save(org3: Org3, receiver: Receiver, url: str, reason: str) -> None:
    resp = await org3.admin.client.post(f"{BASE}/subscriptions", json={"url": url, "event_types": ["placement.started"]}, headers=org3.admin.h())
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "egress_refused"
    assert resp.json()["error"]["details"]["reason"] == reason


async def test_private_resolution_refused_at_connect(org3: Org3, receiver: Receiver) -> None:
    created = await create(org3, url="https://private.example.com/hook")
    row = await sub_row(org3.tenant_id, uuid.UUID(created["subscription"]["id"]))
    assert row.status == "pending_verification"
    assert row.verification_error == "egress_refused:private_address"
    assert receiver.requests == []


async def test_filters_and_types_validated(org3: Org3, receiver: Receiver) -> None:
    bad = await org3.admin.client.post(
        f"{BASE}/subscriptions", json={"url": "https://h.example.com/", "event_types": ["placement.started"], "filters": {"email": ["x"]}}, headers=org3.admin.h()
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_filter"
    unknown = await org3.admin.client.post(f"{BASE}/subscriptions", json={"url": "https://h.example.com/", "event_types": ["no.such_type"]}, headers=org3.admin.h())
    assert unknown.status_code == 422
    ok = await create(org3, event_types=["*"], filters={"client_id": [str(uuid.uuid4())]})
    assert ok["subscription"]["all_events"] is True


async def test_tenant_limit(org3: Org3, receiver: Receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_max_per_tenant", 1)
    await create(org3)
    resp = await org3.admin.client.post(f"{BASE}/subscriptions", json={"url": "https://h.example.com/", "event_types": ["placement.started"]}, headers=org3.admin.h())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "subscription_limit"


async def test_cross_tenant_404(org3: Org3, other3: Org3, receiver: Receiver) -> None:
    created = await create(org3)
    resp = await other3.admin.client.get(f"{BASE}/subscriptions/{created['subscription']['id']}")
    assert resp.status_code == 404


def test_installation_scope_rule() -> None:
    assert common.installation_allows("placement.started", ["placements:read"])
    assert not common.installation_allows("placement.started", ["candidates:read"])
    assert not common.installation_allows("candidate.created", ["pii.contact:read"])
    assert common.installation_allows("app.installation_changed", [])
    assert not common.installation_allows("proposal.decided", ["mcp:read"])


async def test_public_own_only_and_api_keys_refused(app: FastAPI, org3: Org3, receiver: Receiver) -> None:
    scopes = ("placements:read", "webhooks:manage")
    a = await install_app(org3.tenant_id, await make_app(scopes=scopes, resources=("api",)), granted_by=org3.admin.user_id)
    b = await install_app(org3.tenant_id, await make_app(scopes=scopes, resources=("api",)), granted_by=org3.admin.user_id)
    api_a = Api(app, (await mint(a, scopes=scopes)).access_token)
    api_b = Api(app, (await mint(b, scopes=scopes)).access_token)
    created = await api_a.post("/webhooks", {"url": "https://a.example.com/hook", "event_types": ["placement.started"]})
    assert created.status_code == 201, created.text
    sid = created.json()["subscription"]["id"]
    assert created.json()["subscription"]["installation_id"] == str(a.id)
    refused = await api_a.post("/webhooks", {"url": "https://a.example.com/hook", "event_types": ["timesheet.approved"]})
    assert refused.status_code == 422
    assert code(refused) == "event_type_not_allowed"
    with_inst = await api_a.post("/webhooks", {"url": "https://a.example.com/hook", "event_types": ["placement.started"], "installation_id": str(b.id)})
    assert with_inst.status_code == 422
    assert (await api_b.get(f"/webhooks/{sid}")).status_code == 404
    assert (await api_b.get("/webhooks")).json()["data"] == []
    tenant_owned = await create(org3)
    assert (await api_a.get(f"/webhooks/{tenant_owned['subscription']['id']}")).status_code == 404
    got = await api_a.get(f"/webhooks/{sid}")
    assert got.status_code == 200
    assert got.headers["ETag"]
    no_match = await api_a.patch(f"/webhooks/{sid}", {"description": "x"})
    assert no_match.status_code == 428
    patched = await api_a.patch(f"/webhooks/{sid}", {"description": "x"}, if_match=got.headers["ETag"])
    assert patched.status_code == 200, patched.text
    assert patched.json()["description"] == "x"
    no_scope = Api(app, (await mint(a, scopes=("placements:read",))).access_token)
    assert (await no_scope.get("/webhooks")).status_code == 403
    from app.core.permissions import Principal
    from app.core.public_api import scope_satisfied

    key = Principal(type="api_key", id=uuid.uuid4(), tenant_id=org3.tenant_id, permissions={})
    assert not scope_satisfied(key, "webhooks:manage")  # D-24: app-only


def test_actors_refused_by_require_human() -> None:
    from app.core.actors import ActorRef, require_human
    from app.core.errors import Forbidden

    for kind in ("app", "api_key", "agent", "system"):
        with pytest.raises(Forbidden):
            require_human(ActorRef(kind, uuid.uuid4()))  # type: ignore[arg-type]
