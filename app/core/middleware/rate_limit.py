"""Rate-limit middleware (NFR-16; Epic 12 A9) and helpers.

* Endpoint policies (``rate_limit_policies.ENDPOINT_POLICIES``: sign-in, password reset, invitations,
  platform sign-in, MFA) are evaluated here, i.e. **before** any password hashing. The email subject of
  ``auth_login`` / ``auth_reset_request`` is read from the (small, buffered) JSON body and hashed.
* ``ip_anon`` applies to requests carrying no session cookie and no bearer token.
* Over the limit: 429 ``rate_limited`` (AD-12 envelope) with ``Retry-After`` and ``X-RateLimit-*``.
* Client IP: ``X-Forwarded-For`` is honoured only when the direct peer is in ``TRUSTED_PROXIES``
  (``client_ip``); the right-most address that is not a trusted proxy wins.
* ``AfterResponseMiddleware`` runs callbacks registered in ``scope["tos_after_response"]`` with the response
  status once the response is sent (API key request log).
"""

import ipaddress
import json
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import rate_limit, rate_limit_policies
from app.core.errors import error_response
from app.core.settings import get_settings

MAX_BODY = 64 * 1024


def _networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out = []
    for raw in get_settings().trusted_proxies:
        try:
            out.append(ipaddress.ip_network(raw.strip(), strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXIES entry {!r}", raw)
    return out


def _trusted(ip: str, networks: list[Any]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def client_ip(scope: Scope, headers: dict[str, str]) -> str | None:
    client = scope.get("client")
    peer = client[0] if client else None
    networks = _networks()
    if peer is None or not networks or not _trusted(peer, networks):
        return peer
    forwarded = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
    for candidate in reversed(forwarded):
        if not _trusted(candidate, networks):
            return candidate
    return forwarded[0] if forwarded else peer


def _cookies(headers: dict[str, str]) -> set[str]:
    raw = headers.get("cookie", "")
    return {part.split("=", 1)[0].strip() for part in raw.split(";") if "=" in part}


def _cookie_value(headers: dict[str, str], name: str) -> str | None:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


async def _send_429(send: Send, result: rate_limit.RateResult) -> None:
    response = error_response(
        429, "rate_limited", "Too many requests; try again later", {"retry_after": result.retry_after}, headers=result.headers()
    )
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.items()],
        }
    )
    await send({"type": "http.response.body", "body": response.body})


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not get_settings().rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope["method"].upper()
        path = scope["path"]
        ip = client_ip(scope, headers) or "unknown"
        policy = rate_limit_policies.match(method, path)
        replay: list[Message] | None = None

        if policy is not None:
            email: str | None = None
            if any(lim.subject == "email" for lim in policy.limits):
                body, replay = await _read_body(receive)
                email = _email_of(body)
            for lim in policy.limits:
                subject = {
                    "ip": ip,
                    "email": rate_limit.subject_hash(email) if email else None,
                    "mfa_cookie": rate_limit.subject_hash(_cookie_value(headers, "tos_mfa") or ip),
                }[lim.subject]
                if subject is None:
                    continue
                result = await rate_limit.check(f"{policy.name}:{lim.subject}", subject, lim.limit, lim.window_seconds)
                if not result.allowed:
                    await _send_429(send, result)
                    return
        elif path.startswith("/api/") and not (_cookies(headers) & set(rate_limit_policies.AUTH_COOKIES)) and not headers.get(
            "authorization"
        ):
            lim = rate_limit_policies.IP_ANON
            result = await rate_limit.check("ip_anon", ip, lim.limit, lim.window_seconds)
            if not result.allowed:
                await _send_429(send, result)
                return

        if replay is not None:
            messages = list(replay)

            async def replay_receive() -> Message:
                if messages:
                    return messages.pop(0)
                return await receive()

            await self.app(scope, replay_receive, send)
        else:
            await self.app(scope, receive, send)


async def _read_body(receive: Receive) -> tuple[bytes, list[Message]]:
    chunks: list[bytes] = []
    messages: list[Message] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size <= MAX_BODY:
            chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), messages


def _email_of(body: bytes) -> str | None:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None
    value = data.get("email") if isinstance(data, dict) else None
    return str(value).strip().lower() if isinstance(value, str) and value.strip() else None


AfterHook = Callable[[int], Awaitable[None]]


class AfterResponseMiddleware:
    """Runs ``scope["tos_after_response"]`` hooks (``async (status) -> None``) after the response is sent."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            for hook in scope.get("tos_after_response", []):
                try:
                    await hook(status_holder["status"])
                except Exception:
                    logger.exception("After-response hook failed")
