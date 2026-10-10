"""A9 (NFR-16) sliding-window rate limiting + A10 token redaction. Matrix rows 28, 30, 32."""

import logging
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from app.core import rate_limit
from app.core.logger import RedactingFilter, redact
from app.core.middleware.rate_limit import client_ip
from app.core.settings import get_settings
from app.services import auth_service as identity_service

LOGIN = "/api/v1/auth/login"


@pytest.fixture
def limits_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "rate_limit_backend", "memory")
    rate_limit.memory_backend().reset()
    yield
    rate_limit.set_backend(None)
    rate_limit.memory_backend().reset()


async def test_sliding_window_counts_previous_bucket() -> None:
    backend = rate_limit.MemoryBackend()
    # previous bucket fully weighted: 5 old hits + limit 5 -> refused
    for _ in range(5):
        assert (await backend.incr("cur-a", "prev-a", 10, 60_000, 1.0))[0]
    for _ in range(5):
        assert (await backend.incr("cur-b", "cur-a", 10, 60_000, 1.0))[0]
    assert not (await backend.incr("cur-b", "cur-a", 10, 60_000, 1.0))[0]
    # half weight: 5 * 0.5 + 5 = 7.5 -> two more allowed (8.5, 9.5), then refused
    assert (await backend.incr("cur-b", "cur-a", 10, 60_000, 0.5))[0]
    assert (await backend.incr("cur-b", "cur-a", 10, 60_000, 0.5))[0]
    assert not (await backend.incr("cur-b", "cur-a", 10, 60_000, 0.5))[0]


async def test_eleventh_login_is_429_before_hashing(
    app: FastAPI, client: httpx.AsyncClient, limits_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix 28."""
    hashed: list[int] = []
    real = identity_service.verify_password

    def counting(password_hash: str | None, password: str) -> bool:
        hashed.append(1)
        return real(password_hash, password)

    monkeypatch.setattr(identity_service, "verify_password", counting)
    statuses = []
    for i in range(11):
        body = {
            "tenant_slug": f"nope-{uuid.uuid4().hex[:6]}",
            "email": f"u{i}@example.com",
            "password": "Wrong-Pass-123",
        }
        statuses.append((await client.post(LOGIN, json=body)).status_code)
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429
    assert len(hashed) == 10  # the refused request never reached password hashing
    resp = await client.post(
        LOGIN, json={"tenant_slug": "x", "email": "a@example.com", "password": "Wrong-Pass-123"}
    )
    assert resp.json()["error"]["code"] == "rate_limited"
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.headers["X-RateLimit-Limit"] == "10"
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    assert resp.headers["x-request-id"]


async def test_login_per_email_limit(client: httpx.AsyncClient, limits_on: None) -> None:
    body = {"tenant_slug": "acme", "email": "Target@Example.com", "password": "Wrong-Pass-123"}
    statuses = [(await client.post(LOGIN, json=body)).status_code for _ in range(6)]
    assert statuses == [401] * 5 + [429]


async def test_redis_down_falls_back_to_memory(
    client: httpx.AsyncClient, limits_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix 30: requests are still served (and limited) when Redis is unreachable."""
    monkeypatch.setattr(get_settings(), "rate_limit_backend", "redis")
    monkeypatch.setattr(get_settings(), "redis_port", 1)  # nothing listens there
    rate_limit.set_backend(rate_limit.RedisBackend())
    resp = await client.get("/api/v1/health")
    assert resp.status_code in (200, 503)  # served (the DB check decides), not a limiter error
    result = await rate_limit.check("probe", "subject", 2, 60)
    assert result.allowed


def test_client_ip_trusts_forwarded_for_only_from_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = {"client": ("10.0.0.5", 1234)}
    headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.4"}
    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    assert client_ip(scope, headers) == "10.0.0.5"
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.0/8"])
    assert client_ip(scope, headers) == "203.0.113.9"
    assert client_ip({"client": ("198.51.100.1", 1)}, headers) == "198.51.100.1"


def test_hit_alias_is_kept_for_epic6(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    rate_limit.memory_backend().reset()
    results = [asyncio.run(rate_limit.hit("k", 2, 60)) for _ in range(3)]
    assert results == [True, True, False]


def test_log_redaction() -> None:
    """Matrix 32: tokens never reach access logs."""
    token = "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"
    line = f'127.0.0.1 - "GET /api/v1/auth/invitations/{token} HTTP/1.1" 200'
    assert token not in redact(line)
    assert "/accept-invite#token=[redacted]" in redact(f"https://app/accept-invite#token={token}")
    assert redact("GET /l/AbCdEfGhIjKlMnOpQrStUvWxYz012345 410").endswith("/l/[redacted] 410")
    assert "tos_0a1b2c3d" not in redact("Authorization: Bearer tos_0a1b2c3d_" + "x" * 43)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s", ("GET", f"/l/{token[33:]}"), None
    )
    RedactingFilter().filter(record)
    assert token[33:] not in record.getMessage()


def test_redacted_uvicorn_access_line_still_formats() -> None:
    """Regression: the filter used to collapse ``args`` to ``()``, so uvicorn's ``AccessFormatter`` raised
    ``ValueError: not enough values to unpack (expected 5, got 0)`` for every access line carrying a token."""
    from uvicorn.logging import AccessFormatter

    secret = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/api/v1/public/links/{secret}?token={secret}&x=1", "1.1", 200),
        None,
    )
    assert RedactingFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5
    formatter = AccessFormatter(fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    line = formatter.format(record)
    assert secret not in line
    assert "/public/links/[redacted]?token=[redacted]&x=1" in line
    assert line.endswith(" 200 OK")
    # A line without secrets is untouched.
    plain = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("c", "GET", "/api/v1/health", "1.1", 204),
        None,
    )
    RedactingFilter().filter(plain)
    assert plain.args == ("c", "GET", "/api/v1/health", "1.1", 204)


def test_invitation_and_reset_links_use_the_fragment() -> None:
    assert "/accept-invite#token=abc" in identity_service.accept_invite_url("abc")
    assert "/reset-password#token=abc" in identity_service.reset_password_url("abc")
