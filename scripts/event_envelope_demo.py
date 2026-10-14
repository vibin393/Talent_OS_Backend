"""Prove the shared event envelope's consumer semantics (Phase 1, Epic 5.3 / NFR2).

Exercises dedup (same event_id), out-of-order safety (apply-if-newer on subject_version),
tombstone (delete + no resurrection by a late lower-version event), and wire round-trip.
Exit 0 = all semantics hold.
"""

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.event_envelope import (  # noqa: E402
    APPLIED,
    DUPLICATE,
    STALE,
    TOMBSTONED,
    Envelope,
    InMemoryConsumerStore,
    commit,
    decide,
)

TENANT = uuid.uuid4()
SUBJECT = uuid.uuid4()


def _env(version: int, *, tombstone: bool = False, event_id: uuid.UUID | None = None) -> Envelope:
    return Envelope(
        event_id=event_id or uuid.uuid4(),
        tenant_id=TENANT,
        event_type="placement.changed",
        event_version=1,
        subject_type="placement",
        subject_id=SUBJECT,
        subject_version=version,
        occurred_at=datetime.now(tz=UTC),
        producer="talentos",
        tombstone=tombstone,
    )


def _offer(store: InMemoryConsumerStore, env: Envelope) -> str:
    outcome = decide(store, env)
    if outcome in (APPLIED, TOMBSTONED):
        commit(store, env)
    return outcome


def main() -> int:
    store = InMemoryConsumerStore()
    checks: list[tuple[str, bool]] = []

    e1 = _env(1)
    checks.append(("first event applies", _offer(store, e1) == APPLIED))
    checks.append(("same event_id is duplicate", _offer(store, e1) == DUPLICATE))

    checks.append(("newer version (2) applies", _offer(store, _env(2)) == APPLIED))
    checks.append(("older version (1) is stale (out-of-order safe)", _offer(store, _env(1)) == STALE))
    checks.append(("equal version (2) is stale", _offer(store, _env(2)) == STALE))

    checks.append(("tombstone at newer version (3) tombstones", _offer(store, _env(3, tombstone=True)) == TOMBSTONED))
    checks.append(("late lower version (2) does NOT resurrect", _offer(store, _env(2)) == STALE))

    # wire round-trip preserves fields
    env = _env(5, tombstone=True)
    rt = Envelope.from_wire(env.to_wire())
    checks.append(("wire round-trip preserves subject_version+tombstone",
                   rt.subject_version == 5 and rt.tombstone is True and rt.subject_id == SUBJECT))

    passed = sum(1 for _, ok in checks if ok)
    print(f"\nEvent envelope semantics — {passed}/{len(checks)} checks passed\n")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    all_ok = passed == len(checks)
    print("\nRESULT:", "envelope is idempotent + out-of-order safe ✓" if all_ok else "SEMANTICS BROKEN ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
