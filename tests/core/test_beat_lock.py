"""AD-37: only the beat holding the Redis lock sends periodic tasks (fake Redis)."""

from typing import Any

from celery.beat import PersistentScheduler

from app.job.scheduler import IDLE_TICK_SECONDS, LOCK_KEY, BeatLock, LockedScheduler
from app.job.schedules import BEAT_SCHEDULE, BEAT_SCHEDULER


class FakeRedis:
    """Enough of redis-py for ``BeatLock``: SET NX PX, GET, PEXPIRE with a manual clock."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float]] = {}
        self.now = 0.0

    def _alive(self, key: str) -> tuple[str, float] | None:
        item = self.data.get(key)
        if item is None or item[1] <= self.now:
            self.data.pop(key, None)
            return None
        return item

    def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> bool:
        if nx and self._alive(key) is not None:
            return False
        self.data[key] = (value, self.now + (px or 0) / 1000)
        return True

    def get(self, key: str) -> bytes | None:
        item = self._alive(key)
        return item[0].encode() if item else None

    def pexpire(self, key: str, px: int) -> bool:
        item = self._alive(key)
        if item is None:
            return False
        self.data[key] = (item[0], self.now + px / 1000)
        return True


def _scheduler(lock: BeatLock, sent: list[str], name: str) -> LockedScheduler:
    sched = object.__new__(LockedScheduler)
    sched.lock = lock
    sched._last_idle_log = 0.0
    sched._name = name  # type: ignore[attr-defined]
    sched._sent = sent  # type: ignore[attr-defined]
    return sched


def test_only_the_lock_holder_ticks(monkeypatch: Any) -> None:
    sent: list[str] = []

    def fake_tick(self: Any, *args: Any, **kwargs: Any) -> float:
        sent.append(self._name)
        return 1.0

    monkeypatch.setattr(PersistentScheduler, "tick", fake_tick)
    redis = FakeRedis()
    a = _scheduler(BeatLock(redis, token="a"), sent, "a")
    b = _scheduler(BeatLock(redis, token="b"), sent, "b")

    assert a.tick() == 1.0
    assert b.tick() == IDLE_TICK_SECONDS
    redis.now += 30
    a.tick()  # renews
    redis.now += 40  # 70 s after the first acquisition, 40 s after the renewal: still a's
    assert b.tick() == IDLE_TICK_SECONDS
    assert sent == ["a", "a"]
    redis.now += 61  # a died: the lock expires, b takes over
    assert b.tick() == 1.0
    assert sent == ["a", "a", "b"]
    assert redis.get(LOCK_KEY) == b"b"


def test_lock_errors_mean_no_send() -> None:
    class Broken:
        def set(self, *a: Any, **k: Any) -> bool:
            raise ConnectionError("redis down")

    assert BeatLock(Broken()).acquire_or_renew() is False


def test_schedule_uses_the_locked_scheduler() -> None:
    from app.core.celery import celery_app

    assert BEAT_SCHEDULER == "app.job.scheduler:LockedScheduler"
    assert celery_app.conf.beat_scheduler == BEAT_SCHEDULER
    assert BEAT_SCHEDULE["legal-entities-ensure-periods"]["task"] == "legal_entities.ensure_periods_all"
    assert BEAT_SCHEDULE["maintenance-purge-idempotency-keys"]["task"] == "maintenance.purge_idempotency_keys"
