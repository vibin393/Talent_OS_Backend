"""Debounced "dirty id" sets in Redis (Epic 5, AD-9): handlers ``mark(...)`` ids and enqueue one delayed drain
task per key and window; the drain task ``drain(...)``s the set. A beat sweep drains leftovers.

* ``mark(key, ids, lock_ttl)`` -> True when the caller should enqueue the drain task (it acquired the lock
  ``<key>:lock`` for ``lock_ttl`` seconds); False when a drain is already scheduled.
* When Redis is unreachable, ``mark`` returns ``None`` and callers enqueue the work directly with the ids.

Synchronous (used from after-commit event handlers, which are sync). Keys never hold PII: ids only.
"""

from collections.abc import Iterable
from typing import Any

from loguru import logger

from app.core.settings import get_settings

_client = None


def _redis() -> Any:  # redis client typing is loose
    global _client  # process-wide lazy client
    if _client is None:
        import redis

        s = get_settings()
        _client = redis.Redis(
            host=s.redis_host,
            port=s.redis_port,
            password=s.redis_password or None,
            db=2,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=2,
        )
    return _client


def mark(key: str, ids: Iterable[str], lock_ttl: int) -> bool | None:
    members = [str(i) for i in ids]
    try:
        client = _redis()
        if members:
            client.sadd(key, *members)
        return bool(client.set(f"{key}:lock", "1", nx=True, ex=max(1, lock_ttl)))
    except Exception as exc:
        logger.warning("Dirty-set mark failed for {} ({}); enqueueing directly", key, type(exc).__name__)
        return None


def drain(key: str, limit: int = 5000) -> list[str]:
    """Pop up to ``limit`` ids and release the lock so the next change schedules a new drain."""
    try:
        client = _redis()
        popped = client.spop(key, limit) or []
        client.delete(f"{key}:lock")
        return [str(p) for p in popped]
    except Exception as exc:
        logger.warning("Dirty-set drain failed for {} ({})", key, type(exc).__name__)
        return []


def keys(pattern: str) -> list[str]:
    try:
        return [k for k in _redis().scan_iter(match=pattern, count=500) if not str(k).endswith(":lock")]
    except Exception as exc:
        logger.warning("Dirty-set scan failed for {} ({})", pattern, type(exc).__name__)
        return []
