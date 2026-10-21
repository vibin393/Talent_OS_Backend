# ruff: noqa: E501
"""Module gateway (``/api/v1/modules/{module}/{path}``) hardening (unified platform).

Covers the P0 fixes:
* the central token minted for the embedded module now carries the user's real ``module_caps`` (no blanket admin);
* the entitlement gate returns 403 ``module_not_entitled`` when the tenant's plan excludes the module;
* only the header allow-list is forwarded upstream (spoofable ``x-*`` headers are stripped);
* ``_safe_path`` rejects traversal / backslashes / encoded slashes.
"""

import base64
import json
from typing import Any

import pytest

from app.router import modules as gw
from tests.epic3 import org3  # noqa: F401 - fixture


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class _FakeResp:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._body = body or {}
        self.content = content or json.dumps(self._body).encode()
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeClient:
    """Captures the SSO exchange and the proxied request so the test can assert on them."""

    captured: dict[str, Any] = {}

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResp:  # noqa: A002
        _FakeClient.captured["sso_url"] = url
        _FakeClient.captured["central_token"] = (json or {}).get("token")
        return _FakeResp(200, {"access_token": "module-session-token"})

    async def request(self, method: str, url: str, params: Any = None, content: Any = None, headers: dict[str, str] | None = None) -> _FakeResp:
        _FakeClient.captured["proxy_method"] = method
        _FakeClient.captured["proxy_url"] = url
        _FakeClient.captured["fwd_headers"] = dict(headers or {})
        return _FakeResp(200, {"ok": True}, content=b'{"ok":true}')


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _FakeClient.captured = {}
    monkeypatch.setattr(gw.httpx, "AsyncClient", _FakeClient)
    gw._token_cache.clear()
    return _FakeClient


# ================================================================ unit: path safety
def test_safe_path_rejects_traversal_and_tricks() -> None:
    assert gw._safe_path("croar/candidates")
    assert gw._safe_path("v1/jobs/123")
    assert not gw._safe_path("../etc/passwd")
    assert not gw._safe_path("a/../../b")
    assert not gw._safe_path("a\\b")
    assert not gw._safe_path("a%2e%2eb")
    assert not gw._safe_path("a%2fb")
    assert not gw._safe_path("a%5cb")
    assert not gw._safe_path("a\x00b")
    assert not gw._safe_path("")


# ================================================================ integration
async def test_gateway_mints_token_with_module_caps(app: Any, org3: Any, fake_httpx: type[_FakeClient]) -> None:  # noqa: F811
    resp = await org3.admin.client.get("/api/v1/modules/croar/ping")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    # the central token POSTed to the module's SSO endpoint carries the user's real module_caps (admin = all caps)
    central = fake_httpx.captured["central_token"]
    claims = _decode_jwt_payload(central)
    assert "module_caps" in claims, claims
    assert "croar" in claims["module_caps"] and claims["module_caps"]["croar"], claims["module_caps"]
    # the module was called with the Bearer the gateway minted (never the browser's auth)
    assert fake_httpx.captured["fwd_headers"]["Authorization"] == "Bearer module-session-token"


async def test_gateway_header_allow_list(app: Any, org3: Any, fake_httpx: type[_FakeClient]) -> None:  # noqa: F811
    spoof = {
        "X-Api-Key": "attacker-key",
        "X-User-Id": "00000000-0000-0000-0000-000000000000",
        "X-Tenant-Id": "evil",
        "X-Forwarded-For": "1.2.3.4",
        "Accept-Language": "en-IN",
    }
    resp = await org3.admin.client.get("/api/v1/modules/croar/ping", headers=spoof)
    assert resp.status_code == 200, resp.text
    fwd = {k.lower(): v for k, v in fake_httpx.captured["fwd_headers"].items()}
    # allow-listed header passes through; everything spoofable is stripped
    assert fwd.get("accept-language") == "en-IN"
    for banned in ("x-api-key", "x-user-id", "x-tenant-id", "x-forwarded-for", "cookie"):
        assert banned not in fwd, (banned, fwd)
    assert fwd["authorization"] == "Bearer module-session-token"


async def test_gateway_entitlement_gate(app: Any, org3: Any, fake_httpx: type[_FakeClient]) -> None:  # noqa: F811
    from app.services import entitlements

    # sandbox plan excludes 'intelligence' (Croar) but keeps 'experience' (Feedback IQ)
    await entitlements.set_tenant_plan(org3.tenant_id, "sandbox")
    blocked = await org3.admin.client.get("/api/v1/modules/croar/ping")
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["code"] == "module_not_entitled"
    allowed = await org3.admin.client.get("/api/v1/modules/feedbackiq/ping")
    assert allowed.status_code == 200, allowed.text


async def test_gateway_rejects_bad_path(app: Any, org3: Any, fake_httpx: type[_FakeClient]) -> None:  # noqa: F811
    resp = await org3.admin.client.get("/api/v1/modules/croar/a%5c..%5cb")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "invalid_module_path"
