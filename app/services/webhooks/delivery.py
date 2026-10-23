# ruff: file-ignore[line-too-long, non-imperative-mood]
"""One HTTP attempt (Epic 68, §0.5, AD-80): ``SafeHttpClient`` with no redirects, a 64 KB read cap, 5 s connect /
10 s total; the Egress Guard runs on every connection. Sandbox echo URLs are delivered in-process (no network loop).

Tests install a fake transport with ``set_transport`` (an ``httpx.MockTransport``) and a stub resolver
(``core.net_safety.set_resolver``): tests never call real URLs."""

import dataclasses
import re
import time
import uuid
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.net_safety import EgressRefused, SafeHttpClient
from app.core.settings import get_settings
from app.services.webhooks.common import echo_token_of, now

MAX_BYTES = 65_536
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Egress reasons that are a transient / receiver problem (retried on the ladder) rather than a security signal.
_RETRYABLE_EGRESS = {"timeout": "timeout", "redirect": "redirect_not_followed", "too_large": "response_too_large", "resolution": "connection"}

_transport_factory: dict[str, Callable[[], httpx.AsyncBaseTransport] | None] = {"fn": None}


def set_transport(fn: Callable[[], httpx.AsyncBaseTransport] | None) -> None:
    """Tests only: every client is built with ``fn()`` as its transport (``None`` restores the real one)."""
    _transport_factory["fn"] = fn


@dataclasses.dataclass(slots=True)
class Outcome:
    status_code: int | None
    latency_ms: int
    error: str | None = None  # timeout | connection | redirect_not_followed | egress_refused:<reason> | http_<code> | tls
    excerpt: str | None = None
    body: bytes = b""
    retry_after: int | None = None
    security_refusal: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def excerpt(content: bytes) -> str:
    limit = get_settings().webhook_response_excerpt_bytes
    text = content[:limit].decode("utf-8", errors="replace")
    return _CONTROL.sub("", text)


def retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None  # type: ignore[unreachable]
    return max(0, int((when - now()).total_seconds()))


def client() -> SafeHttpClient:
    s = get_settings()
    fn = _transport_factory["fn"]
    return SafeHttpClient(
        max_redirects=0, max_bytes=MAX_BYTES, connect_timeout=s.webhook_connect_timeout_seconds,
        total_timeout=s.webhook_timeout_seconds, purpose="webhook", transport=fn() if fn is not None else None,
    )  # fmt: skip


async def post(db: AsyncSession, tenant_id: uuid.UUID, url: str, body: bytes, headers: Mapping[str, str], *, sandbox: bool) -> Outcome:
    start = time.monotonic()
    token = echo_token_of(url)
    if token is not None:
        if not sandbox:
            return Outcome(None, 0, error="egress_refused:host_not_allowed", security_refusal=True)
        from app.services.webhooks import echo

        status, content = await echo.receive(db, tenant_id, token, dict(headers), body)
        ms = int((time.monotonic() - start) * 1000)
        return Outcome(status, ms, None if 200 <= status < 300 else f"http_{status}", excerpt(content), content)
    try:
        async with client() as c:
            resp = await c.request("POST", url, headers=dict(headers), content=body)
    except EgressRefused as exc:
        ms = int((time.monotonic() - start) * 1000)
        mapped = _RETRYABLE_EGRESS.get(exc.reason)
        if mapped is not None:
            return Outcome(None, ms, error=mapped)
        return Outcome(None, ms, error=f"egress_refused:{exc.reason}", security_refusal=True)
    except httpx.ConnectError:
        return Outcome(None, int((time.monotonic() - start) * 1000), error="connection")
    except httpx.TransportError as exc:
        err = "tls" if "ssl" in type(exc).__name__.lower() or "certificate" in str(exc).lower() else "connection"
        return Outcome(None, int((time.monotonic() - start) * 1000), error=err)
    ms = int((time.monotonic() - start) * 1000)
    code = resp.status_code
    err = None if 200 <= code < 300 else ("redirect_not_followed" if 300 <= code < 400 else f"http_{code}")  # type: ignore[assignment]
    return Outcome(
        code, ms, err, excerpt(resp.content), resp.content,
        retry_after=retry_after_seconds(resp.headers.get("retry-after")),
    )  # fmt: skip


__all__ = ["MAX_BYTES", "Outcome", "client", "excerpt", "post", "retry_after_seconds", "set_transport"]
