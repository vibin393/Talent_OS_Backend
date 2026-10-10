"""Malware scanning port (AD-13, NFR-15).

``scan(data, filename)`` returns a ``ScanResult``:

* ``clean`` / ``infected`` -- from ClamAV ``clamd`` (INSTREAM over TCP) when ``settings.clamav_host`` is set;
* ``error`` -- the scanner could not be reached or answered unexpectedly (callers refuse the upload);
* ``skipped_dev`` -- no scanner configured and ``app_env`` is development/test. Any other environment
  without a scanner yields ``error``.

The filename is never logged (it may contain personal data).
"""

import asyncio
import contextlib
import dataclasses
import struct
from typing import Literal

from loguru import logger

from app.core.settings import get_settings

ScanStatus = Literal["clean", "infected", "skipped_dev", "error"]

_CHUNK = 64 * 1024
_TIMEOUT_SECONDS = 30


@dataclasses.dataclass(frozen=True, slots=True)
class ScanResult:
    status: ScanStatus
    detail: str | None = None


async def _clamd_instream(host: str, port: int, data: bytes) -> ScanResult:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), _TIMEOUT_SECONDS)
    try:
        writer.write(b"zINSTREAM\0")
        for start in range(0, len(data), _CHUNK):
            chunk = data[start : start + _CHUNK]
            writer.write(struct.pack("!I", len(chunk)) + chunk)
        writer.write(struct.pack("!I", 0))
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(4096), _TIMEOUT_SECONDS)
    finally:
        writer.close()
        with contextlib.suppress(OSError):  # best effort close
            await writer.wait_closed()
    text = reply.rstrip(b"\0\n").decode("utf-8", "replace")
    if text.endswith("OK"):
        return ScanResult("clean")
    if text.endswith("FOUND"):
        signature = text.removeprefix("stream:").removesuffix("FOUND").strip()
        return ScanResult("infected", signature or None)
    return ScanResult("error", text[:200] or "empty reply")


async def scan(data: bytes, filename: str) -> ScanResult:  # noqa: ARG001 - kept for scanners that use it
    s = get_settings()
    if not s.clamav_host:
        if s.app_env in {"development", "test"}:
            return ScanResult("skipped_dev", "no scanner configured")
        return ScanResult("error", "no scanner configured")
    try:
        result = await _clamd_instream(s.clamav_host, s.clamav_port, data)
    except (OSError, TimeoutError) as exc:
        logger.warning("Malware scanner unavailable: {}", type(exc).__name__)
        return ScanResult("error", "scanner unavailable")
    if result.status == "infected":
        logger.warning("Malware scanner flagged an upload ({} bytes)", len(data))
    return result
