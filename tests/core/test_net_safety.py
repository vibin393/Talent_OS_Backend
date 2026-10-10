"""Story 66.7: Egress Guard (FR-337, NFR-62). Stub resolver + httpx.MockTransport; no network, no DB."""

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

import httpcore
import httpx
import pytest

from app.core import net_safety
from app.core.net_safety import (
    EgressRefused,
    SafeHttpClient,
    is_public,
    resolve_public,
    set_denied_cidrs,
    set_resolver,
    validate_url,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


class StubResolver:
    def __init__(self, table: dict[str, Sequence[str] | list[Sequence[str]]] | None = None) -> None:
        self.table = table or {}
        self.calls: list[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.calls.append(host)
        answer = self.table.get(host)
        if answer is None:
            raise OSError("NXDOMAIN")
        if answer and isinstance(answer[0], list):  # sequence of answers, one per call (rebinding)
            seq = answer
            idx = min(sum(1 for c in self.calls if c == host) - 1, len(seq) - 1)
            return seq[idx]
        return answer  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    set_resolver(None)
    set_denied_cidrs([])


def _assert_no_ip(exc: EgressRefused, *ips: str) -> None:
    text = f"{exc.message} {exc.details} {exc!s} {exc!r}"
    for ip in ips:
        assert ip not in text


# ------------------------------------------------------------------------------------------- validate_url


def test_validate_url_happy_path_normalises() -> None:
    p = validate_url("HTTPS://API.Example.COM./v1/x?a=1#frag")
    assert (p.scheme, p.host, p.port, p.path_qs) == ("https", "api.example.com", 443, "/v1/x?a=1")
    assert p.url == "https://api.example.com/v1/x?a=1"
    p2 = validate_url("https://api.example.com:8443")
    assert (p2.port, p2.path_qs, p2.url) == (8443, "/", "https://api.example.com:8443/")


def test_validate_url_idna() -> None:
    assert validate_url("https://bücher.example/").host == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api.example.com/", "scheme"),
        ("ftp://api.example.com/", "scheme"),
        ("api.example.com/x", "scheme"),
        ("https://api.example.com:80/", "port"),
        ("https://api.example.com:22/", "port"),
        ("https://api.example.com:99999/", "port"),
        ("https://user:pass@api.example.com/", "userinfo"),
        ("https://user@api.example.com/", "userinfo"),
        ("https://127.0.0.1/", "ip_literal"),
        ("https://93.184.216.34/", "ip_literal"),
        ("https://[::1]/", "ip_literal"),
        ("https://[::ffff:127.0.0.1]/", "ip_literal"),
        ("https://2130706433/", "ip_literal"),
        ("https://0177.0.0.1/", "ip_literal"),
        ("https://0x7f.1/", "ip_literal"),
        ("https://0x7f000001/", "ip_literal"),
        ("https://127.1/", "ip_literal"),
        ("https://localhost/", "suffix"),
        ("https://api.localhost/", "suffix"),
        ("https://printer.local/", "suffix"),
        ("https://metadata.google.internal/", "suffix"),
        ("https://nas.lan/", "suffix"),
        ("https://router.home.arpa/", "suffix"),
        ("https://intranet.corp/", "suffix"),
        ("https://singlelabel/", "suffix"),
        ("https:///path", "suffix"),
        ("https://ⓛⓞⓒⓐⓛⓗⓞⓢⓣ/", "suffix"),
    ],
)
def test_validate_url_refusals(url: str, reason: str) -> None:
    with pytest.raises(EgressRefused) as ei:
        validate_url(url)
    assert ei.value.details == {"reason": reason}
    assert ei.value.status_code == 422
    assert ei.value.code == "egress_refused"


def test_allowed_hosts_exact_and_wildcard() -> None:
    allowed = ["api.example.com", "*.partner.io"]
    assert validate_url("https://api.example.com/", allowed_hosts=allowed).host == "api.example.com"
    assert validate_url("https://eu.api.partner.io/", allowed_hosts=allowed).host == "eu.api.partner.io"
    for url in ("https://partner.io/", "https://other.example.com/", "https://evilpartner.io/"):
        with pytest.raises(EgressRefused) as ei:
            validate_url(url, allowed_hosts=allowed)
        assert ei.value.details["reason"] == "host_not_allowed"


def test_custom_ports() -> None:
    assert validate_url("https://api.example.com:9443/", ports=frozenset({9443})).port == 9443
    with pytest.raises(EgressRefused):
        validate_url("https://api.example.com/", ports=frozenset({9443}))


def test_unknown_reason_rejected() -> None:
    with pytest.raises(ValueError):
        EgressRefused("nope")


# ------------------------------------------------------------------------------------------------ is_public

NON_PUBLIC = [
    # IPv4
    "0.0.0.0",
    "0.1.2.3",
    "10.0.0.1",
    "10.255.255.255",
    "100.64.0.1",
    "100.127.255.254",
    "127.0.0.1",
    "127.255.255.254",
    "169.254.169.254",
    "169.254.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.0.0.1",
    "192.0.2.1",
    "192.88.99.1",
    "192.168.1.1",
    "198.18.0.1",
    "198.51.100.1",
    "203.0.113.1",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    # IPv6
    "::",
    "::1",
    "::127.0.0.1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:7f00:1",
    "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe",
    "64:ff9b:1::1",
    "100::1",
    "2001::1",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "2001:db8::1",
    "2002:7f00:1::1",
    "2002:c0a8:101::1",
    "2002:5db8:d822::1",
    "fc00::1",
    "fd12:3456::1",
    "fe80::1",
    "fe80::1%1",
    "fec0::1",
    "ff02::1",
    "not-an-ip",
]


@pytest.mark.parametrize("ip", NON_PUBLIC)
def test_non_public_addresses(ip: str) -> None:
    assert not is_public(ip)
    if "%" not in ip and ip != "not-an-ip":
        assert not is_public(ipaddress.ip_address(ip))


@pytest.mark.parametrize(
    "ip", [PUBLIC_V4, "8.8.8.8", "1.1.1.1", PUBLIC_V6, "::ffff:8.8.8.8", "64:ff9b::808:808"]
)
def test_public_addresses(ip: str) -> None:
    assert is_public(ip)


def test_extra_denied_cidrs() -> None:
    assert is_public("8.8.8.8")
    set_denied_cidrs(["8.8.8.0/24", "2606:2800::/32", " "])
    assert net_safety.get_denied_cidrs() == ["8.8.8.0/24", "2606:2800::/32"]
    assert not is_public("8.8.8.8")
    assert not is_public("::ffff:8.8.8.8")
    assert not is_public(PUBLIC_V6)
    assert is_public("1.1.1.1")


# --------------------------------------------------------------------------------------------- resolve_public


async def test_resolve_public_ok_and_refusals() -> None:
    set_resolver(
        StubResolver(
            {
                "ok.example.com": [PUBLIC_V4, PUBLIC_V6, PUBLIC_V4],
                "mixed.example.com": [PUBLIC_V4, "10.0.0.5"],
                "meta.example.com": ["169.254.169.254"],
                "empty.example.com": [],
                "junk.example.com": ["not-an-ip"],
            }
        )
    )
    assert await resolve_public("ok.example.com") == (
        ipaddress.ip_address(PUBLIC_V4),
        ipaddress.ip_address(PUBLIC_V6),
    )
    for host, reason in [
        ("mixed.example.com", "private_address"),
        ("meta.example.com", "private_address"),
        ("empty.example.com", "resolution"),
        ("junk.example.com", "resolution"),
        ("nx.example.com", "resolution"),
    ]:
        with pytest.raises(EgressRefused) as ei:
            await resolve_public(host)
        assert ei.value.details == {"reason": reason}
        _assert_no_ip(ei.value, "10.0.0.5", "169.254.169.254")


# -------------------------------------------------------------------------------------------- SafeHttpClient


def _transport(handler: Callable[[httpx.Request], Any]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_request_pins_ip_and_keeps_host_header() -> None:
    set_resolver(StubResolver({"api.example.com": [PUBLIC_V4]}))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    async with SafeHttpClient(purpose="test", transport=_transport(handler)) as client:
        resp = await client.post("https://api.example.com/hook", json={"a": 1}, params={"q": "x"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert resp.url == "https://api.example.com/hook?q=x"
    req = seen[0]
    assert req.url.host == PUBLIC_V4
    assert req.url.port is None or req.url.port == 443
    assert req.url.path == "/hook" and req.url.query == b"q=x"
    assert req.headers["host"] == "api.example.com"
    assert req.extensions["sni_hostname"] == "api.example.com"
    assert client.requested_hosts == ["api.example.com"]


async def test_request_nonstandard_port_host_header_and_ipv6_pin() -> None:
    set_resolver(StubResolver({"api.example.com": [PUBLIC_V6]}))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with SafeHttpClient(purpose="test", transport=_transport(handler)) as client:
        resp = await client.get("https://api.example.com:8443/x")
    assert resp.status_code == 204 and resp.content == b""
    assert seen[0].url.host == PUBLIC_V6 and seen[0].url.port == 8443
    assert seen[0].headers["host"] == "api.example.com:8443"


async def test_dns_rebinding_second_resolution_private_refused() -> None:
    resolver = StubResolver({"rebind.example.com": [[PUBLIC_V4], ["127.0.0.1"]]})
    set_resolver(resolver)
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(200, text="ok")

    async with SafeHttpClient(purpose="test", transport=_transport(handler)) as client:
        assert (await client.get("https://rebind.example.com/")).text == "ok"
        with pytest.raises(EgressRefused) as ei:
            await client.get("https://rebind.example.com/")
    assert ei.value.details == {"reason": "private_address"}
    _assert_no_ip(ei.value, "127.0.0.1")
    assert hits == [PUBLIC_V4]  # the private answer was never connected to
    assert client.requested_hosts == ["rebind.example.com", "rebind.example.com"]


async def test_mixed_public_private_answer_refused() -> None:
    set_resolver(StubResolver({"mixed.example.com": [PUBLIC_V4, "192.168.0.10"]}))
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with SafeHttpClient(purpose="test", transport=_transport(handler)) as client:
        with pytest.raises(EgressRefused) as ei:
            await client.get("https://mixed.example.com/")
    assert ei.value.details == {"reason": "private_address"}
    _assert_no_ip(ei.value, "192.168.0.10", PUBLIC_V4)
    assert not called


async def test_syntax_refusals_in_client_never_resolve() -> None:
    resolver = StubResolver({})
    set_resolver(resolver)
    async with SafeHttpClient(purpose="test", transport=_transport(lambda r: httpx.Response(200))) as client:
        for url, reason in [
            ("http://api.example.com/", "scheme"),
            ("https://2130706433/", "ip_literal"),
            ("https://0177.0.0.1/", "ip_literal"),
            ("https://0x7f.1/", "ip_literal"),
            ("https://127.1/", "ip_literal"),
            ("https://u:p@api.example.com/", "userinfo"),
            ("https://api.example.com:22/", "port"),
            ("https://metadata.google.internal/computeMetadata/v1/", "suffix"),
        ]:
            with pytest.raises(EgressRefused) as ei:
                await client.get(url)
            assert ei.value.details == {"reason": reason}
    assert resolver.calls == []


async def test_allowed_hosts_in_client_records_attempt() -> None:
    set_resolver(StubResolver({"evil.example.com": [PUBLIC_V4], "api.partner.io": [PUBLIC_V4]}))
    async with SafeHttpClient(
        purpose="connector",
        allowed_hosts=["*.partner.io"],
        transport=_transport(lambda r: httpx.Response(200)),
    ) as client:
        assert (await client.get("https://api.partner.io/")).status_code == 200
        with pytest.raises(EgressRefused) as ei:
            await client.get("https://evil.example.com/")
    assert ei.value.details == {"reason": "host_not_allowed"}
    assert client.requested_hosts == ["api.partner.io", "evil.example.com"]


async def test_redirect_followed_and_revalidated() -> None:
    set_resolver(StubResolver({"a.example.com": [PUBLIC_V4], "b.example.com": ["1.1.1.1"]}))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["host"] == "a.example.com":
            return httpx.Response(302, headers={"location": "https://b.example.com/final"})
        return httpx.Response(200, text="done")

    async with SafeHttpClient(purpose="connector", max_redirects=3, transport=_transport(handler)) as client:
        resp = await client.post(
            "https://a.example.com/start", content=b"x", headers={"Authorization": "Bearer t"}
        )
    assert resp.text == "done" and resp.url == "https://b.example.com/final"
    assert client.requested_hosts == ["a.example.com", "b.example.com"]
    second = seen[1]
    assert second.method == "GET" and second.url.host == "1.1.1.1"
    assert "authorization" not in second.headers  # credentials not forwarded cross-host


async def test_redirect_to_private_host_refused() -> None:
    set_resolver(StubResolver({"a.example.com": [PUBLIC_V4], "internal.example.com": ["10.1.2.3"]}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://internal.example.com/admin"})

    async with SafeHttpClient(purpose="connector", max_redirects=3, transport=_transport(handler)) as client:
        with pytest.raises(EgressRefused) as ei:
            await client.get("https://a.example.com/")
    assert ei.value.details == {"reason": "private_address"}
    _assert_no_ip(ei.value, "10.1.2.3")


@pytest.mark.parametrize(
    "location", ["http://b.example.com/", "https://169.254.169.254/latest/meta-data/", "https://localhost/"]
)
async def test_redirect_to_bad_url_refused(location: str) -> None:
    set_resolver(StubResolver({"a.example.com": [PUBLIC_V4]}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": location})

    async with SafeHttpClient(purpose="connector", max_redirects=3, transport=_transport(handler)) as client:
        with pytest.raises(EgressRefused) as ei:
            await client.get("https://a.example.com/")
    assert ei.value.details == {"reason": "redirect"}
    _assert_no_ip(ei.value, "169.254.169.254")


async def test_redirect_beyond_limit_refused() -> None:
    set_resolver(StubResolver({"a.example.com": [PUBLIC_V4]}))
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(301, headers={"location": f"/hop{count}"})

    async with SafeHttpClient(purpose="connector", max_redirects=3, transport=_transport(handler)) as client:
        with pytest.raises(EgressRefused) as ei:
            await client.get("https://a.example.com/")
    assert ei.value.details == {"reason": "redirect"}
    assert count == 4

    async with SafeHttpClient(purpose="webhook", transport=_transport(handler)) as webhook:
        with pytest.raises(EgressRefused) as ei:
            await webhook.get("https://a.example.com/")
    assert ei.value.details == {"reason": "redirect"}


async def test_oversized_body_refused() -> None:
    set_resolver(StubResolver({"big.example.com": [PUBLIC_V4]}))

    async def stream() -> AsyncIterator[bytes]:
        for _ in range(11):
            yield b"x" * 100

    def declared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1001)

    def streamed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream())  # no Content-Length

    for handler in (declared, streamed):
        async with SafeHttpClient(purpose="test", max_bytes=1000, transport=_transport(handler)) as client:
            with pytest.raises(EgressRefused) as ei:
                await client.get("https://big.example.com/")
        assert ei.value.details == {"reason": "too_large"}

    async with SafeHttpClient(
        purpose="test",
        max_bytes=1000,
        transport=_transport(lambda r: httpx.Response(200, content=b"x" * 1000)),
    ) as client:
        assert len((await client.get("https://big.example.com/")).content) == 1000


async def test_timeouts() -> None:
    set_resolver(StubResolver({"slow.example.com": [PUBLIC_V4]}))

    def raises(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async def sleeps(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    for handler in (raises, sleeps):
        async with SafeHttpClient(
            purpose="test", total_timeout=0.05, transport=_transport(handler)
        ) as client:
            with pytest.raises(EgressRefused) as ei:
                await client.get("https://slow.example.com/")
        assert ei.value.details == {"reason": "timeout"}


async def test_connect_error_does_not_leak_address() -> None:
    set_resolver(StubResolver({"down.example.com": [PUBLIC_V4]}))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url.host}", request=request)

    async with SafeHttpClient(purpose="test", transport=_transport(handler)) as client:
        with pytest.raises(httpx.ConnectError) as ei:
            await client.get("https://down.example.com/")
    assert PUBLIC_V4 not in str(ei.value)
    assert ei.value.__cause__ is None


async def test_client_ignores_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    client = SafeHttpClient(purpose="test")
    try:
        assert type(client._transport._pool) is httpcore.AsyncConnectionPool  # no proxy pool
    finally:
        await client.aclose()


def test_safe_response_text_charset() -> None:
    r = net_safety.SafeResponse(
        200, httpx.Headers({"content-type": "text/plain; charset=latin-1"}), "café".encode("latin-1"), "u"
    )
    assert r.text == "café" and r.is_success
