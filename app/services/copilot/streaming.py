# ruff: file-ignore[line-too-long]
"""Copilot turn streams (Epic 54 §0.4, AD-64). The runtime's step task writes ``text.delta`` to the Redis Stream
``agents:stream:<run_id>`` (MAXLEN ~2000, TTL 15 min); the Copilot adds ``turn.started``, ``tool.finished``,
``draft.created`` and ``turn.completed`` / ``turn.failed``, and serves the stream as SSE (the Redis entry id is the SSE
``id:``, so ``Last-Event-ID`` resumes).

Tool events (Known gap 2): the merged runtime publishes only ``text.delta``, so ``tool.finished`` / ``draft.created``
are derived from the run log (``get_run``) -- while the SSE is open (``poll`` every second) and before
``turn.completed`` -- and de-duplicated through the set ``agents:stream:<run_id>:seen``. ``tool.started`` is not shown.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from loguru import logger
from redis.asyncio import Redis

from app.core.settings import get_settings

STREAM_MAXLEN = 2000
STREAM_TTL_SECONDS = 900
FINAL_EVENTS = frozenset({"turn.completed", "turn.failed"})
POLL_SECONDS = 1.0
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def redis_client() -> Redis:
    s = get_settings()
    return Redis(
        host=s.redis_host,
        port=s.redis_port,
        db=0,
        password=s.redis_password or None,
        decode_responses=True,
        socket_timeout=5,
    )


def stream_key(run_id: uuid.UUID | str) -> str:
    return f"agents:stream:{run_id}"


def _seen_key(run_id: uuid.UUID | str) -> str:
    return f"agents:stream:{run_id}:seen"


async def publish(
    run_id: uuid.UUID | str, event: str, data: Mapping[str, Any], *, once: str | None = None
) -> bool:
    """XADD one event (best effort). ``once`` = a de-duplication token: the event is added only the first time."""
    client = redis_client()
    try:
        if once is not None:
            if not await client.sadd(_seen_key(run_id), once):
                return False
            await client.expire(_seen_key(run_id), STREAM_TTL_SECONDS)
        key = stream_key(run_id)
        await client.xadd(
            key,
            {"event": event, "data": json.dumps(dict(data), default=str)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        await client.expire(key, STREAM_TTL_SECONDS)
        return True
    except Exception:
        logger.debug("Copilot stream publish failed for run {}", run_id)
        return False
    finally:
        await client.aclose()


async def entries(run_id: uuid.UUID | str) -> list[tuple[str, str, dict[str, Any]]]:
    """All stored entries ``(id, event, data)`` of a run's stream (oldest first)."""
    client = redis_client()
    try:
        raw = await client.xrange(stream_key(run_id), "-", "+")
    except Exception:
        return []
    finally:
        await client.aclose()
    return [(eid, f.get("event", ""), _loads(f.get("data"))) for eid, f in raw]


async def delta_text(run_id: uuid.UUID | str) -> str:
    return "".join(str(d.get("text") or "") for _, ev, d in await entries(run_id) if ev == "text.delta")


def _loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def frame(event_id: str, event: str, data: Mapping[str, Any] | str) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(dict(data), default=str, separators=(",", ":"))
    lines = [f"id: {event_id}", f"event: {event}"]
    lines.extend(f"data: {line}" for line in payload.splitlines() or [""])
    return ("\n".join(lines) + "\n\n").encode()


async def sse(
    run_id: uuid.UUID,
    *,
    last_event_id: str | None,
    poll: Callable[[], Awaitable[None]] | None = None,
    heartbeat_s: float | None = None,
    timeout_s: float | None = None,
) -> AsyncIterator[bytes]:
    """SSE frames of a turn: replay from ``last_event_id`` then follow live entries, with ``: ping`` heartbeats; ends
    after ``turn.completed`` / ``turn.failed``, at the turn timeout or when the stream expired."""
    s = get_settings()
    heartbeat = float(heartbeat_s or s.copilot_sse_heartbeat_seconds)
    deadline = time.monotonic() + float(timeout_s or s.copilot_turn_timeout_seconds) + 30
    last = last_event_id or "0-0"
    key = stream_key(run_id)
    client = redis_client()
    next_ping = time.monotonic() + heartbeat
    next_poll = time.monotonic()
    missing_since: float | None = None
    yield b"retry: 2000\n\n"
    try:
        while True:
            now = time.monotonic()
            if poll is not None and now >= next_poll:
                next_poll = now + POLL_SECONDS
                try:
                    await poll()
                except Exception:
                    logger.debug("Copilot stream poll failed for run {}", run_id)
            block_ms = int(max(50, min(1000, (next_ping - time.monotonic()) * 1000)))
            try:
                resp = await client.xread({key: last}, count=100, block=block_ms)
            except Exception:
                logger.debug("Copilot stream read failed for run {}", run_id)
                return
            done = False
            for _, items in resp or []:
                for eid, fields in items:
                    last = eid
                    event = fields.get("event", "message")
                    yield frame(eid, event, fields.get("data") or "{}")
                    done = done or event in FINAL_EVENTS
            if done:
                return
            now = time.monotonic()
            if now >= next_ping:
                next_ping = now + heartbeat
                yield b": ping\n\n"
            if now >= deadline:
                return
            if not resp:
                if not await client.exists(key):
                    missing_since = missing_since or now
                    if now - missing_since > 10:
                        return  # expired (or never created)
                else:
                    missing_since = None
    finally:
        await client.aclose()


__all__ = [
    "FINAL_EVENTS",
    "SSE_HEADERS",
    "delta_text",
    "entries",
    "frame",
    "publish",
    "redis_client",
    "sse",
    "stream_key",
]
