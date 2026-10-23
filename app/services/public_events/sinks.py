# ruff: file-ignore[line-too-long]
"""Outbox sink registry (Epic 66, AD-79). Owners register from their ``hooks.py``: ``connector_triggers`` (66),
``webhooks`` (68), later consumers (79). Sinks must be idempotent (at-least-once delivery)."""

import dataclasses
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.registry import Registry
from app.services.public_events.outbox import OutboxRow

SinkFn = Callable[[RequestContext, AsyncSession, Sequence[OutboxRow]], Awaitable[None]]


@dataclasses.dataclass(frozen=True, slots=True)
class Sink:
    name: str
    fn: SinkFn
    event_types: frozenset[str] | None = None
    include_internal: bool = False

    def select(self, rows: Sequence[OutboxRow]) -> list[OutboxRow]:
        return [
            r
            for r in rows
            if (self.include_internal or r.visibility == "public")
            and (self.event_types is None or r.event_type in self.event_types)
        ]


_sinks: Registry[str, Sink] = Registry("outbox sink")


def register_sink(
    name: str, fn: SinkFn, *, event_types: frozenset[str] | None = None, include_internal: bool = False
) -> None:
    _sinks.register(
        name, Sink(name, fn, frozenset(event_types) if event_types is not None else None, include_internal)
    )


def unregister_sink(name: str) -> None:
    """Remove a sink (tests only)."""
    _sinks.unregister(name)


def sinks() -> list[Sink]:
    return [_sinks.get(k) for k in sorted(_sinks.view())]


__all__ = ["Sink", "SinkFn", "register_sink", "sinks", "unregister_sink"]
