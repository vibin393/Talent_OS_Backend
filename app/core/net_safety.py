"""Egress Guard (Story 66.7, AD-80, FR-337, NFR-62).

Every outbound call to a Tenant- or partner-supplied URL goes through :class:`SafeHttpClient`, which

* validates the URL syntax (``https`` only, allowed ports, no userinfo, no IP literal in any encoding, a
  public-looking suffix, optional host allow-list) — :func:`validate_url`;
* resolves the host and refuses if ANY address is non-public — :func:`resolve_public` / :func:`is_public`;
* connects to the checked address (pinned IP; ``Host`` header and TLS SNI stay the hostname), so a DNS answer
  that changes between check and connect (rebinding) can never reach an internal address;
* follows redirects manually (each hop re-validated and re-resolved), caps the body and enforces timeouts;
* ignores proxy environment variables (``trust_env=False``).

Refusals raise :class:`EgressRefused` (422 ``egress_refused``) whose message/details never contain the
resolved address. This module is pure infrastructure: it imports nothing from ``app.services`` and does not
read settings — the caller wires ``settings.egress_denied_cidrs`` through :func:`set_denied_cidrs`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json as _json
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.errors import DomainError

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = Callable[[str], Awaitable[Sequence[str | IPAddress]]]

EGRESS_REASONS = frozenset(
    {
        "scheme",
        "port",
        "userinfo",
        "ip_literal",
        "suffix",
        "resolution",
        "private_address",
        "host_not_allowed",
        "redirect",
        "too_large",
        "timeout",
    }
)

_DEFAULT_MESSAGES: dict[str, str] = {
    "scheme": "Only https URLs are allowed",
    "port": "The URL port is not allowed",
    "userinfo": "URLs must not contain credentials",
    "ip_literal": "The URL host must be a domain name, not an IP address",
    "suffix": "The URL host is not a public domain name",
    "resolution": "The URL host could not be resolved",
    "private_address": "The URL host resolves to a non-public address",
    "host_not_allowed": "The URL host is not in the allowed hosts",
    "redirect": "The redirect was refused",
    "too_large": "The response body is too large",
    "timeout": "The outbound request timed out",
}


class EgressRefused(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - public name fixed by the architecture
    """An outbound call refused by the Egress Guard. ``details.reason`` says why; never carries an address."""

    status_code = 422
    code = "egress_refused"
    default_message = "The outbound request was refused"

    def __init__(self, reason: str, message: str | None = None) -> None:
        if reason not in EGRESS_REASONS:
            raise ValueError(f"unknown egress refusal reason: {reason}")
        self.reason = reason
        super().__init__(message or _DEFAULT_MESSAGES[reason], details={"reason": reason})


# ------------------------------------------------------------------------------------------------ URL syntax


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    host: str  # lowercase, IDNA (punycode) normalised, no trailing dot
    port: int
    path_qs: str  # path + "?query" (no fragment); "/" when empty
    url: str  # normalised URL: https://host[:port]path_qs


_DENIED_SUFFIXES = (
    "localhost",
    "local",
    "localdomain",
    "internal",
    "intranet",
    "lan",
    "home.arpa",
    "arpa",
    "corp",
)
_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
# A label an inet_aton / WHATWG URL parser reads as a number: decimal, octal (leading 0) or hex (0x).
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")


def _normalise_host(raw: str) -> str:
    host = raw.strip().rstrip(".").lower()
    if not host:
        raise EgressRefused("suffix", "The URL has no host")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise EgressRefused("suffix", "The URL host is not a valid domain name") from None
    return host.rstrip(".")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    if ":" in host:
        return True
    # inet_aton / WHATWG semantics: when the last label is numeric the whole host is parsed as IPv4
    # (``2130706433``, ``0177.0.0.1``, ``0x7f.1``, ``127.1``) — refuse any such host.
    labels = host.split(".")
    return bool(_NUMERIC_LABEL_RE.match(labels[-1]))


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.strip().rstrip(".").lower()
    if pattern.startswith("*."):
        suffix = _normalise_pattern(pattern[2:])
        return host.endswith("." + suffix)
    return host == _normalise_pattern(pattern)


def _normalise_pattern(pattern: str) -> str:
    try:
        return pattern.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return pattern


def check_allowed(host: str, allowed_hosts: Sequence[str] | None) -> None:
    """Raise ``host_not_allowed`` unless ``host`` matches an exact entry or a ``*.suffix`` pattern."""
    if allowed_hosts is None:
        return
    if not any(_host_matches(host, p) for p in allowed_hosts):
        raise EgressRefused("host_not_allowed")


def validate_url(
    url: str, *, allowed_hosts: Sequence[str] | None = None, ports: frozenset[int] = frozenset({443, 8443})
) -> ParsedUrl:
    """Syntax rules only (no DNS). Raises :class:`EgressRefused`."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise EgressRefused("suffix", "The URL is malformed") from None
    scheme = parts.scheme.lower()
    if scheme != "https":
        raise EgressRefused("scheme")
    netloc = parts.netloc
    if "@" in netloc or parts.username is not None or parts.password is not None:
        raise EgressRefused("userinfo")
    if netloc.startswith("[") or "[" in netloc:
        raise EgressRefused("ip_literal")
    try:
        port = parts.port
    except ValueError:
        raise EgressRefused("port") from None
    port = 443 if port is None else port
    if port not in ports:
        raise EgressRefused("port")
    raw_host = parts.hostname or ""
    if any(c in raw_host for c in "\\%\x00 \t\r\n"):
        raise EgressRefused("suffix", "The URL host is not a valid domain name")
    host = _normalise_host(raw_host)
    if _is_ip_literal(host):
        raise EgressRefused("ip_literal")
    if len(host) > 253:
        raise EgressRefused("suffix", "The URL host is not a valid domain name")
    labels = host.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        if _is_ip_literal(host):
            raise EgressRefused("ip_literal")
        raise EgressRefused("suffix", "The URL host is not a valid domain name")
    if len(labels) < 2 or any(host == s or host.endswith("." + s) for s in _DENIED_SUFFIXES):
        raise EgressRefused("suffix")
    check_allowed(host, allowed_hosts)
    path_qs = parts.path or "/"
    if parts.query:
        path_qs = f"{path_qs}?{parts.query}"
    authority = host if port == 443 else f"{host}:{port}"
    return ParsedUrl(
        scheme=scheme, host=host, port=port, path_qs=path_qs, url=f"https://{authority}{path_qs}"
    )


# --------------------------------------------------------------------------------------------- address checks

_DENIED_V4 = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",  # "this" network
        "10.0.0.0/8",  # RFC 1918  # NOSONAR
        "100.64.0.0/10",  # carrier-grade NAT  # NOSONAR
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local (incl. 169.254.169.254 metadata)  # NOSONAR
        "172.16.0.0/12",  # RFC 1918  # NOSONAR
        "192.0.0.0/24",  # IETF protocol assignments  # NOSONAR
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast  # NOSONAR
        "192.168.0.0/16",  # RFC 1918  # NOSONAR
        "198.18.0.0/15",  # benchmarking  # NOSONAR
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast  # NOSONAR
        "240.0.0.0/4",  # reserved (incl. 255.255.255.255 broadcast)  # NOSONAR
    )
)
_DENIED_V6 = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "::/96",  # IPv4-compatible (deprecated)
        "::ffff:0:0/96",  # IPv4-mapped (handled via embedding first)
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments (incl. Teredo 2001::/32)
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4
        "3fff::/20",  # documentation
        "5f00::/16",  # SRv6 SIDs
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated)
        "ff00::/8",  # multicast
    )
)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_TEREDO = ipaddress.IPv6Network("2001::/32")

_extra_denied: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def set_denied_cidrs(cidrs: Sequence[str]) -> None:
    """Platform ranges (``settings.egress_denied_cidrs``) refused in addition to the built-in list."""
    _extra_denied[:] = [ipaddress.ip_network(c.strip(), strict=False) for c in cidrs if c.strip()]


def get_denied_cidrs() -> list[str]:
    return [str(n) for n in _extra_denied]


def _in_extra(ip: IPAddress) -> bool:
    return any(ip.version == n.version and ip in n for n in _extra_denied)


def _v4_public(ip: ipaddress.IPv4Address) -> bool:
    if any(ip in n for n in _DENIED_V4):
        return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return not _in_extra(ip)


def is_public(ip: IPAddress | str) -> bool:
    """Return True only for a global unicast address outside the built-in and platform deny lists."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return False
    if isinstance(ip, ipaddress.IPv4Address):
        return _v4_public(ip)
    if ip.scope_id:
        return False
    if ip.ipv4_mapped is not None:
        return _v4_public(ip.ipv4_mapped) and not _in_extra(ip)
    if ip in _TEREDO or ip.teredo is not None:
        return False
    if ip.sixtofour is not None:
        return False  # 6to4 is deprecated; the embedded address cannot be trusted to stay public
    if ip in _NAT64:
        embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        return _v4_public(embedded) and not _in_extra(ip)
    if any(ip in n for n in _DENIED_V6):
        return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_site_local:
        return False
    return not _in_extra(ip)


# ------------------------------------------------------------------------------------------------- resolution


async def _default_resolver(host: str) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


_resolver: Resolver = _default_resolver


def set_resolver(fn: Resolver | None) -> None:
    """Install a resolver (tests use a stub); ``None`` restores the system resolver."""
    global _resolver
    _resolver = fn if fn is not None else _default_resolver


async def resolve_public(host: str) -> tuple[IPAddress, ...]:
    """Resolve ``host``; refuse (``private_address``) if ANY answer is non-public."""
    try:
        answers = await _resolver(host)
    except (OSError, UnicodeError, ValueError):
        raise EgressRefused("resolution") from None
    addresses: list[IPAddress] = []
    for answer in answers:
        if isinstance(answer, ipaddress.IPv4Address | ipaddress.IPv6Address):
            addr = answer
        else:
            try:
                addr = ipaddress.ip_address(str(answer).split("%", 1)[0])
            except ValueError:
                raise EgressRefused("resolution") from None
        if addr not in addresses:
            addresses.append(addr)
    if not addresses:
        raise EgressRefused("resolution")
    if not all(is_public(a) for a in addresses):
        raise EgressRefused("private_address")
    return tuple(addresses)


# ---------------------------------------------------------------------------------------------------- client


class SafeResponse:
    """A fully read, size-capped response. ``url`` is the hostname URL (never the pinned address)."""

    def __init__(self, status_code: int, headers: httpx.Headers, content: bytes, url: str) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.url = url

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        for part in ctype.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip("\"'")
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.content)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def __repr__(self) -> str:
        return f"<SafeResponse [{self.status_code}] {self.url}>"


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_HEADERS = ("authorization", "cookie", "proxy-authorization")


class SafeHttpClient:
    """Outbound HTTP client enforcing the Egress Guard on every request and redirect hop."""

    def __init__(
        self,
        *,
        allowed_hosts: Sequence[str] | None = None,
        max_redirects: int = 0,
        max_bytes: int = 1_048_576,
        connect_timeout: float = 5.0,
        total_timeout: float = 10.0,
        purpose: str,
        transport: httpx.AsyncBaseTransport | None = None,
        ports: frozenset[int] = frozenset({443, 8443}),
    ) -> None:
        self.allowed_hosts = list(allowed_hosts) if allowed_hosts is not None else None
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.purpose = purpose
        self.ports = ports
        self.requested_hosts: list[str] = []
        self._timeout = httpx.Timeout(total_timeout, connect=connect_timeout).as_dict()
        # The transport is used directly (no httpx.AsyncClient): no proxy env vars (trust_env is a client
        # concern), no automatic redirects, no cookie jar and no httpx request logging of the pinned address.
        self._transport = transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def get(self, url: str, **kw: Any) -> SafeResponse:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> SafeResponse:
        return await self.request("POST", url, **kw)

    async def request(self, method: str, url: str, **kw: Any) -> SafeResponse:
        """Send ``method url``; ``kw`` accepts ``headers``, ``params``, ``json``, ``content``, ``data``."""
        params = kw.pop("params", None)
        if params:
            url = str(httpx.URL(url, params=params))
        try:
            async with asyncio.timeout(self.total_timeout):
                return await self._request(method.upper(), url, **kw)
        except TimeoutError:
            raise EgressRefused("timeout") from None

    async def _check(self, url: str) -> tuple[ParsedUrl, tuple[IPAddress, ...]]:
        parsed = validate_url(url, ports=self.ports)
        self.requested_hosts.append(parsed.host)
        check_allowed(parsed.host, self.allowed_hosts)
        return parsed, await resolve_public(parsed.host)

    async def _request(self, method: str, url: str, **kw: Any) -> SafeResponse:
        headers = httpx.Headers(kw.pop("headers", None))
        body_kw = {k: kw.pop(k) for k in ("json", "content", "data", "files") if k in kw}
        if kw:
            raise TypeError(f"unsupported arguments: {sorted(kw)}")
        hops = 0
        while True:
            parsed, addresses = await self._check(url)
            response, content = await self._send(method, parsed, addresses, headers, body_kw)
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_STATUSES and location:
                if hops >= self.max_redirects:
                    raise EgressRefused(
                        "redirect", "The destination redirected and redirects are not allowed here"
                    )
                hops += 1
                next_url = urljoin(parsed.url, location)
                try:
                    next_host = validate_url(next_url, ports=self.ports).host
                except EgressRefused as exc:
                    raise EgressRefused("redirect", f"The redirect was refused ({exc.reason})") from None
                if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                    method = "GET" if method != "HEAD" else method
                    body_kw = {}
                    headers = httpx.Headers({k: v for k, v in headers.items() if k.lower() != "content-type"})
                if next_host != parsed.host:
                    headers = httpx.Headers(
                        {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}
                    )
                url = next_url
                continue
            return SafeResponse(response.status_code, response.headers, content, parsed.url)

    async def _send(
        self,
        method: str,
        parsed: ParsedUrl,
        addresses: tuple[IPAddress, ...],
        headers: httpx.Headers,
        body_kw: dict[str, Any],
    ) -> tuple[httpx.Response, bytes]:
        send_headers = httpx.Headers(headers)
        send_headers.setdefault("User-Agent", f"TalentOS-Egress/1 ({self.purpose})")
        send_headers["Host"] = parsed.host if parsed.port == 443 else f"{parsed.host}:{parsed.port}"
        for address in addresses:
            pinned = httpx.URL(parsed.url).copy_with(host=str(address))
            request = httpx.Request(
                method,
                pinned,
                headers=send_headers,
                extensions={"sni_hostname": parsed.host, "timeout": self._timeout},
                **body_kw,
            )
            try:
                response = await self._transport.handle_async_request(request)
            except httpx.TimeoutException:
                raise EgressRefused("timeout") from None
            except httpx.ConnectError:
                continue  # try the next checked address; never surface the address itself
            except httpx.TransportError as exc:
                raise type(exc)(f"{self.purpose}: request to {parsed.host} failed") from None
            response.request = request
            try:
                return response, await self._read_capped(response)
            finally:
                await response.aclose()
        raise httpx.ConnectError(f"{self.purpose}: could not connect to {parsed.host}") from None

    async def _read_capped(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None and declared.strip().isdigit() and int(declared) > self.max_bytes:
            raise EgressRefused("too_large")
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self.max_bytes:
                    raise EgressRefused("too_large")
                chunks.append(chunk)
        except httpx.TimeoutException:
            raise EgressRefused("timeout") from None
        return b"".join(chunks)


__all__ = [
    "EGRESS_REASONS",
    "EgressRefused",
    "IPAddress",
    "ParsedUrl",
    "Resolver",
    "SafeHttpClient",
    "SafeResponse",
    "check_allowed",
    "get_denied_cidrs",
    "is_public",
    "resolve_public",
    "set_denied_cidrs",
    "set_resolver",
    "validate_url",
]
