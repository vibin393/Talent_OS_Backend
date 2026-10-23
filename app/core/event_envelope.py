"""Shared cross-service event envelope + consumer idempotency (Phase 1, Epic 5.3; NFR2).

Every cross-service event carries this envelope so consumers can be **idempotent** (dedup on
``event_id``) and **out-of-order safe** (apply-if-newer on a per-subject monotonic ``subject_version``,
with tombstones for deletes). Producers write it transactionally via the existing outbox pattern
(``app/services/public_events``); this module defines the contract + the consumer-side applier so the
semantics live in one tested place rather than being re-implemented per consumer.
"""

import dataclasses
import json
import uuid
from datetime import datetime
from typing import Any, Protocol


@dataclasses.dataclass(frozen=True, slots=True)
class Envelope:
    event_id: uuid.UUID  # globally unique; the consumer dedup key
    tenant_id: uuid.UUID
    event_type: str  # "<domain>.<verb>"
    event_version: int  # schema version of `data`
    subject_type: str  # e.g. "placement"
    subject_id: uuid.UUID  # the aggregate this event is about
    subject_version: int  # MONOTONIC per (subject_type, subject_id): the apply-if-newer key
    occurred_at: datetime
    producer: str  # source service/module
    tombstone: bool = False  # subject deleted/erased -> consumers drop their projection
    data: dict[str, Any] = dataclasses.field(default_factory=dict)

    def subject_key(self) -> str:
        return f"{self.subject_type}:{self.subject_id}"

    def to_wire(self) -> str:
        """Canonical JSON (sorted keys, compact) — signable/replayable, matches public_events."""
        payload = {
            "event_id": str(self.event_id),
            "tenant_id": str(self.tenant_id),
            "event_type": self.event_type,
            "event_version": self.event_version,
            "subject_type": self.subject_type,
            "subject_id": str(self.subject_id),
            "subject_version": self.subject_version,
            "occurred_at": self.occurred_at.isoformat(),
            "producer": self.producer,
            "tombstone": self.tombstone,
            "data": {} if self.tombstone else self.data,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_wire(cls, raw: str) -> "Envelope":
        d = json.loads(raw)
        return cls(
            event_id=uuid.UUID(d["event_id"]),
            tenant_id=uuid.UUID(d["tenant_id"]),
            event_type=d["event_type"],
            event_version=int(d["event_version"]),
            subject_type=d["subject_type"],
            subject_id=uuid.UUID(d["subject_id"]),
            subject_version=int(d["subject_version"]),
            occurred_at=datetime.fromisoformat(d["occurred_at"]),
            producer=d["producer"],
            tombstone=bool(d.get("tombstone", False)),
            data=d.get("data", {}) or {},
        )


class ConsumerStore(Protocol):
    """Per-consumer durable state the applier needs. A real impl is a DB table per (tenant);
    see the recommended `inbound_events_seen` + per-subject watermark in the Epic 5 design."""

    def seen(self, tenant_id: uuid.UUID, event_id: uuid.UUID) -> bool: ...
    def mark_seen(self, tenant_id: uuid.UUID, event_id: uuid.UUID) -> None: ...
    def applied_version(self, tenant_id: uuid.UUID, subject_key: str) -> int | None: ...
    def set_version(self, tenant_id: uuid.UUID, subject_key: str, version: int) -> None: ...


@dataclasses.dataclass
class InMemoryConsumerStore:
    """Reference ConsumerStore for tests/dev (a real consumer swaps in a DB-backed store)."""

    _seen: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    _versions: dict[tuple[str, str], int] = dataclasses.field(default_factory=dict)

    def seen(self, tenant_id: uuid.UUID, event_id: uuid.UUID) -> bool:
        return (str(tenant_id), str(event_id)) in self._seen

    def mark_seen(self, tenant_id: uuid.UUID, event_id: uuid.UUID) -> None:
        self._seen.add((str(tenant_id), str(event_id)))

    def applied_version(self, tenant_id: uuid.UUID, subject_key: str) -> int | None:
        return self._versions.get((str(tenant_id), subject_key))

    def set_version(self, tenant_id: uuid.UUID, subject_key: str, version: int) -> None:
        self._versions[str(tenant_id), subject_key] = version


# Outcome of offering an event to the applier.
APPLIED = "applied"  # newer than what we have -> caller should upsert the projection
TOMBSTONED = "tombstoned"  # newest + tombstone -> caller should delete the projection
DUPLICATE = "duplicate"  # already processed this event_id -> ack + drop
STALE = "stale"  # older than what we have (out of order / replay) -> ack + drop


def decide(store: ConsumerStore, env: Envelope) -> str:
    """Idempotency + ordering decision for one event. Pure given the store; the caller performs the
    projection write/delete only for APPLIED/TOMBSTONED, then (for those) calls :func:`commit`."""
    if store.seen(env.tenant_id, env.event_id):
        return DUPLICATE
    current = store.applied_version(env.tenant_id, env.subject_key())
    if current is not None and env.subject_version <= current:
        # out-of-order or replay; mark seen so we don't reconsider, but don't move the watermark back
        store.mark_seen(env.tenant_id, env.event_id)
        return STALE
    return TOMBSTONED if env.tombstone else APPLIED


def commit(store: ConsumerStore, env: Envelope) -> None:
    """Record that an APPLIED/TOMBSTONED event was handled: dedup mark + advance the watermark."""
    store.mark_seen(env.tenant_id, env.event_id)
    store.set_version(env.tenant_id, env.subject_key(), env.subject_version)
