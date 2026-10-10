"""Single-beat guard (Epic 13, AD-37): exactly one Celery beat may send periodic tasks.

``LockedScheduler`` holds the Redis lock ``talent_os:beat_lock`` (``SET NX PX 60000``) and renews it on every
tick; a beat process that does not hold the lock sends nothing (and logs that once per minute). If the holder
dies, the lock expires after 60 s and another beat takes over.
"""

import time
import uuid
from typing import Any

from celery.beat import PersistentScheduler
from loguru import logger

LOCK_KEY = "talent_os:beat_lock"
LOCK_TTL_MS = 60_000
IDLE_TICK_SECONDS = 5.0
LOG_EVERY_SECONDS = 60.0


class BeatLock:
    """``SET NX PX`` lock owned by one token; ``acquire_or_renew`` is called on every beat tick."""

    def __init__(self, client: Any, *, key: str = LOCK_KEY, ttl_ms: int = LOCK_TTL_MS, token: str | None = None) -> None:
        self.client = client
        self.key = key
        self.ttl_ms = ttl_ms
        self.token = token or uuid.uuid4().hex

    def acquire_or_renew(self) -> bool:
        try:
            if self.client.set(self.key, self.token, nx=True, px=self.ttl_ms):
                return True
            current = self.client.get(self.key)
            if isinstance(current, bytes):
                current = current.decode()
            if current == self.token:
                self.client.pexpire(self.key, self.ttl_ms)
                return True
        except Exception:
            logger.exception("Beat lock check failed; not sending this tick")
        return False


def _redis_client() -> Any:
    import redis

    from app.core.settings import get_settings

    return redis.Redis.from_url(get_settings().redis_url(0))


class LockedScheduler(PersistentScheduler):  # type: ignore[misc]
    lock: BeatLock | None = None
    _last_idle_log: float = 0.0

    def _beat_lock(self) -> BeatLock:
        if self.lock is None:
            self.lock = BeatLock(_redis_client())
        return self.lock

    def tick(self, *args: Any, **kwargs: Any) -> float:
        if not self._beat_lock().acquire_or_renew():
            now = time.monotonic()
            if now - self._last_idle_log >= LOG_EVERY_SECONDS:
                self._last_idle_log = now
                logger.info("Another beat holds {}; this scheduler sends nothing", LOCK_KEY)
            return IDLE_TICK_SECONDS
        return float(super().tick(*args, **kwargs))
