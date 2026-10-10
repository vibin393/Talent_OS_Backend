"""AD-13 scanning port: dev skip, production refusal without a scanner, ClamAV INSTREAM protocol."""

import asyncio
import struct

import pytest

from app.core import scanning
from app.core.settings import get_settings


async def test_no_scanner_in_test_env_is_skipped_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "clamav_host", "")
    result = await scanning.scan(b"data", "cv.pdf")
    assert result.status == "skipped_dev"


async def test_no_scanner_in_production_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "clamav_host", "")
    monkeypatch.setattr(s, "app_env", "production")
    assert (await scanning.scan(b"data", "cv.pdf")).status == "error"


async def _fake_clamd(reply: bytes) -> tuple[asyncio.AbstractServer, int, list[bytes]]:
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        cmd = await reader.readexactly(len(b"zINSTREAM\0"))
        received.append(cmd)
        body = b""
        while True:
            (size,) = struct.unpack("!I", await reader.readexactly(4))
            if size == 0:
                break
            body += await reader.readexactly(size)
        received.append(body)
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, received


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (b"stream: OK\0", "clean"),
        (b"stream: Eicar-Test-Signature FOUND\0", "infected"),
        (b"weird\0", "error"),
    ],
)
async def test_clamd_instream(monkeypatch: pytest.MonkeyPatch, reply: bytes, status: str) -> None:
    server, port, received = await _fake_clamd(reply)
    s = get_settings()
    monkeypatch.setattr(s, "clamav_host", "127.0.0.1")
    monkeypatch.setattr(s, "clamav_port", port)
    async with server:
        result = await scanning.scan(b"x" * 100_000, "cv.pdf")
    assert result.status == status
    assert received == [b"zINSTREAM\0", b"x" * 100_000]


async def test_unreachable_scanner_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "clamav_host", "127.0.0.1")
    monkeypatch.setattr(s, "clamav_port", 1)
    assert (await scanning.scan(b"x", "cv.pdf")).status == "error"
