"""Erasure-handler registry (Epic 12, A3.2; FR-68).

Each domain that stores candidate personal data registers one handler per concern in
``app/services/<domain>_erasure.py`` (loaded by ``app.services.load_erasure_handlers``). A handler touches only
its own domain's models/services. ``erasure.py`` owns ordering, execution and verification only.

* ``erase`` -- idempotent anonymisation/deletion; returns counts.
* ``export`` -- the subject's data for an access DSR (or ``None``).
* ``verify`` -- residual-PII findings (``[]`` = clean).
* ``tables`` -- the tables the handler claims; ``tests/privacy/test_erasure_coverage.py`` requires every
  candidate-bearing table to be claimed (or listed in ``NON_PERSONAL_TABLES`` with a justification).

Order: ``candidates.profile`` 10, ``candidates.merges`` 20, ``files.candidate`` 30, ``pipeline.*`` 40-41,
``privacy.consents`` 45, ``privacy.records`` 46, ``activity.entity`` 50, ``interviews/offers`` 55-56,
``ai.actions`` 60, ``search.index`` 70, ``portal.account`` 80, ``audit.shred`` 1000 (always last).

Registered today: every handler listed above (``pipeline.screenings`` / ``pipeline.submissions`` -- Epic 6,
``search.index`` -- Epic 5, ``interviews.scorecards`` / ``offers.documents`` -- Epics 7-8, ``portal.account`` --
Epic 9).

Still pending (tracked by ``PENDING_HANDLER_TABLES`` in ``tests/privacy/test_erasure_coverage.py``):

* logged emails (``email_messages``, ``email_recipients``) -- ``activity.entity`` / ``comms``;
* ``notifications`` and ``tasks`` texts naming the candidate -- Epic 10.
* ``demands`` / ``approvals`` hold no candidate data today; if they gain a candidate reference they need a
  handler too.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class ErasureContext:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    run_id: uuid.UUID
    reason: Literal["dsr", "retention"]
    # Epic 13 (R15): ids per table that must be kept (retained by a retention class, e.g. referenced by a
    # retained financial record). Phase 2 erasure handlers honour it; Epic 35 fills it.
    retained: Mapping[str, set[uuid.UUID]] = dataclasses.field(default_factory=dict)
    # Epic 35 (A3): the data subject. ``candidate`` subjects keep ``candidate_id`` (P1 handlers unchanged);
    # ``subject_id`` defaults to it.
    subject_kind: Literal["candidate", "contact", "client_user", "vendor_user"] = "candidate"
    subject_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.subject_id is None:
            object.__setattr__(self, "subject_id", self.candidate_id)


class ErasureHandler(Protocol):
    # Read-only members: handlers declare these as plain class attributes. Properties (no setter) keep them
    # covariant, so a handler's concrete ``tables`` (e.g. ``tuple[str, str]``) satisfies ``tuple[str, ...]``.
    @property
    def name(self) -> str: ...  # "pipeline.screenings"
    @property
    def order(self) -> int: ...  # audit shred = 1000, always last
    @property
    def tables(self) -> tuple[str, ...]: ...  # claimed tables (coverage guard)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]: ...

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None: ...

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]: ...


_handlers: dict[str, ErasureHandler] = {}


def register_erasure_handler(h: ErasureHandler) -> None:
    """Register (or replace, e.g. on module reload) a handler by name."""
    if not h.name or not isinstance(h.order, int):
        msg = f"invalid erasure handler {h!r}"
        raise ValueError(msg)
    _handlers[h.name] = h


def unregister_erasure_handler(name: str) -> None:
    _handlers.pop(name, None)


def handlers() -> list[ErasureHandler]:
    """Return the registered handlers in execution order (ties broken by name)."""
    from app.services import load_erasure_handlers

    load_erasure_handlers()
    return sorted(_handlers.values(), key=lambda h: (h.order, h.name))


def get_handler(name: str) -> ErasureHandler | None:
    return {h.name: h for h in handlers()}.get(name)


def claimed_tables() -> dict[str, str]:
    """``{table: handler name}`` over every registered handler."""
    return {table: h.name for h in handlers() for table in h.tables}


# ---------------------------------------------------------------- activity signals (retention)
# ``(db, candidate_ids) -> {candidate_id: last activity}``: domains report the latest time a candidate was
# "active" (activity entry, stage change, consent grant, portal sign-in) for the retention policy.
ActivitySignal = Callable[[AsyncSession, list[uuid.UUID]], Awaitable[dict[uuid.UUID, datetime]]]
_signals: dict[str, ActivitySignal] = {}


def register_activity_signal(name: str, fn: ActivitySignal) -> None:
    _signals[name] = fn


def activity_signals() -> list[tuple[str, ActivitySignal]]:
    from app.services import load_erasure_handlers

    load_erasure_handlers()
    return sorted(_signals.items())


# ---------------------------------------------------------------- retention exclusions
# ``(db, candidate_ids) -> ids to exclude`` (open applications, portal accounts in use, ...).
RetentionExclusion = Callable[[AsyncSession, list[uuid.UUID]], Awaitable[set[uuid.UUID]]]
_exclusions: dict[str, RetentionExclusion] = {}


def register_retention_exclusion(name: str, fn: RetentionExclusion) -> None:
    _exclusions[name] = fn


def retention_exclusions() -> list[tuple[str, RetentionExclusion]]:
    from app.services import load_erasure_handlers

    load_erasure_handlers()
    return sorted(_exclusions.items())


# ---------------------------------------------------------------- AI-derived data purgers (consent withdrawal)
# ``(db, candidate_id) -> counts`` and ``(db) -> candidate ids that still have derived rows``.
DerivedPurger = Callable[[AsyncSession, uuid.UUID], Awaitable[dict[str, int]]]
DerivedFinder = Callable[[AsyncSession, Iterable[uuid.UUID]], Awaitable[set[uuid.UUID]]]
_purgers: dict[str, tuple[DerivedPurger, DerivedFinder]] = {}


def register_ai_derived_purger(name: str, purge: DerivedPurger, find: DerivedFinder) -> None:
    """Epic 5 registers embeddings / match explanations; Epic 3 bulk import items; etc."""
    _purgers[name] = (purge, find)


def ai_derived_purgers() -> list[tuple[str, DerivedPurger, DerivedFinder]]:
    from app.services import load_erasure_handlers

    load_erasure_handlers()
    return [(name, p, f) for name, (p, f) in sorted(_purgers.items())]
