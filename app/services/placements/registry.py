"""Placement registries (Epic 13, IMPLEMENTED; AD-25 / R8 / R18).

* Start-gate kinds: upstream domains (BGV, onboarding, compliance, commercials) register the gates a Placement
  must pass before joining, optionally with a resolver computing the gate states of a placement.
* Offboarding verifiers: Epics 25 / 27 / 28 / 29 register ``final_timesheet_approved``,
  ``final_expense_settlement``, ``final_payroll_inclusion`` and ``final_invoice``; Epic 31 consumes them.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.registry import Registry


GateStatus = Literal["pending", "met", "failed", "waived", "not_required"]


@dataclasses.dataclass(frozen=True)
class GateState:
    source_ref: str
    status: GateStatus
    required: bool
    detail: Mapping[str, Any]


# (ctx, db, placement_id) -> the gate states of that placement for one gate kind
GateResolver = Callable[[RequestContext, AsyncSession, uuid.UUID], Awaitable[list[GateState]]]


@dataclasses.dataclass(frozen=True)
class StartGateKindDef:
    label: str
    overridable: bool
    resolver: GateResolver | None = None


@dataclasses.dataclass(frozen=True)
class VerifierResult:
    done: bool
    details: Mapping[str, Any]


# (session, placement_id, last_working_day)
OffboardingVerifier = Callable[[AsyncSession, uuid.UUID, date], Awaitable[VerifierResult]]

_gate_kinds: Registry[str, StartGateKindDef] = Registry("start gate kind")
_verifiers: Registry[str, OffboardingVerifier] = Registry("offboarding verifier")


def register_start_gate_kind(kind: str, definition: StartGateKindDef) -> None:
    _gate_kinds.register(kind, definition)


def start_gate_kinds() -> Mapping[str, StartGateKindDef]:
    return _gate_kinds.view()


# ---------------------------------------------------------------- Epic 19 registries (consumed by upper domains)
# (ctx, db, candidate_id) -> JSON-able section of Worker 360 (timesheets, leave, expenses, credentials, ...)
Worker360Loader = Callable[[RequestContext, AsyncSession, uuid.UUID], Awaitable[Any]]
# (stmt, value) -> stmt narrowed by a registered list filter (e.g. ``timesheet_missing`` from ``time``)
PlacementFilter = Callable[[Any, str], Any]

_worker_sections: Registry[str, Worker360Loader] = Registry("worker 360 section", allow_replace=True)
_placement_filters: Registry[str, PlacementFilter] = Registry("placement filter", allow_replace=True)


def register_worker_360_section(key: str, loader: Worker360Loader) -> None:
    _worker_sections.register(key, loader)


def worker_360_sections() -> Mapping[str, Worker360Loader]:
    return _worker_sections.view()


def register_placement_filter(key: str, fn: PlacementFilter) -> None:
    _placement_filters.register(key, fn)


def placement_filters() -> Mapping[str, PlacementFilter]:
    return _placement_filters.view()


def register_offboarding_verifier(kind: str, fn: OffboardingVerifier) -> None:
    _verifiers.register(kind, fn)


def offboarding_verifiers() -> Mapping[str, OffboardingVerifier]:
    return _verifiers.view()
