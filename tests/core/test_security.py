import hashlib

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.errors import Forbidden, register_exception_handlers
from app.core.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    USER_COOKIE,
    csrf_token_for,
    hash_password,
    hash_token,
    new_token,
    password_needs_rehash,
    verify_csrf,
    verify_password,
)


def test_password_hash_is_argon2id_and_verifies() -> None:
    h = hash_password("a long enough password")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "a long enough password")
    assert not verify_password(h, "wrong password")
    assert not verify_password(None, "a long enough password")
    assert not verify_password("not-a-hash", "x")
    assert not password_needs_rehash(h)


def test_tokens_are_random_and_only_hashes_are_stable() -> None:
    a, b = new_token(), new_token()
    assert a != b
    assert len(a) >= 43  # 32 bytes url-safe base64
    assert hash_token(a) == hashlib.sha256(a.encode()).hexdigest()
    assert len(hash_token(a)) == 64


def _csrf_app() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.api_route("/x", methods=["GET", "POST", "DELETE"])
    async def x(request: Request) -> dict[str, bool]:
        verify_csrf(request)
        return {"ok": True}

    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "cookie", "header", "status"),
    [
        ("GET", None, None, 200),
        ("POST", "tok", "tok", 200),
        ("DELETE", "tok", "tok", 200),
        ("POST", None, None, 403),
        ("POST", "tok", None, 403),
        ("POST", None, "tok", 403),
        ("POST", "tok", "other", 403),
        ("POST", "", "", 403),
    ],
)
def test_verify_csrf(method: str, cookie: str | None, header: str | None, status: int) -> None:
    client = _csrf_app()
    # Story 22.9: "tok" stands for the token bound to the presented session.
    session = "session-a"
    client.cookies.set(USER_COOKIE, session)
    bound = csrf_token_for(session)
    if cookie is not None:
        client.cookies.set(CSRF_COOKIE, bound if cookie == "tok" else cookie)
    if header == "tok":
        header = bound
    headers = {CSRF_HEADER: header} if header is not None else {}
    resp = client.request(method, "/x", headers=headers)
    assert resp.status_code == status
    if status == 403:
        assert resp.json()["error"]["code"] == "csrf_failed"


def test_csrf_error_is_forbidden() -> None:
    from app.core.security import csrf_failed

    assert isinstance(csrf_failed(), Forbidden)


# ---------------------------------------------------------------- story 22.9 (A10): session-bound CSRF
def _post(cookies: dict[str, str], header: str) -> int:
    client = _csrf_app()
    for k, v in cookies.items():
        client.cookies.set(k, v)
    return client.post("/x", headers={CSRF_HEADER: header}).status_code


def test_csrf_token_from_session_a_is_rejected_on_session_b() -> None:
    a, b = new_token(), new_token()
    token_a = csrf_token_for(a)
    assert token_a != csrf_token_for(b) and token_a == csrf_token_for(a)
    assert _post({USER_COOKIE: a, CSRF_COOKIE: token_a}, token_a) == 200
    resp_status = _post({USER_COOKIE: b, CSRF_COOKIE: token_a}, token_a)
    assert resp_status == 403


def test_unbound_double_submit_and_sessionless_requests_are_rejected() -> None:
    assert _post({USER_COOKIE: new_token(), CSRF_COOKIE: "anything"}, "anything") == 403
    token = new_token()
    assert _post({CSRF_COOKIE: csrf_token_for(token)}, csrf_token_for(token)) == 403


@pytest.mark.parametrize(
    "cookie_name", ["tos_session", "tos_candidate", "tos_platform", "tos_client", "tos_vendor"]
)
def test_csrf_binds_to_every_principal_cookie(cookie_name: str) -> None:
    token = new_token()
    assert _post({cookie_name: token, CSRF_COOKIE: csrf_token_for(token)}, csrf_token_for(token)) == 200
